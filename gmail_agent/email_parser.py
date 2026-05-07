from __future__ import annotations

import base64
import email.header
import email.utils
import re
from dataclasses import dataclass
from html.parser import HTMLParser
from typing import List, Optional, Tuple


@dataclass
class EmailData:
    message_id: str
    thread_id: str

    # Sender info
    from_addr: str       # "Name <email@domain.com>" raw header
    from_name: str       # "Name"
    from_email: str      # "email@domain.com"
    from_domain: str     # "domain.com"

    to_addr: str
    subject: str
    date: str

    # Content
    snippet: str         # Gmail's 200-char snippet
    body_text: str       # cleaned plain text, max 2000 chars

    # Gmail metadata
    labels: List[str]

    # Attachment metadata (filenames only — contents never touched)
    has_attachments: bool
    attachment_names: List[str]

    # Pre-extracted signals for rules engine
    has_unsubscribe_link: bool
    is_bulk_sender: bool     # List-ID or Precedence: bulk header present
    reply_to_differs: bool   # Reply-To != From domain
    is_thread_reply: bool    # message is part of a thread with >1 message


class EmailParser:
    def parse(self, raw_message: dict, thread_message_count: int = 1) -> EmailData:
        payload = raw_message.get("payload", {})
        headers = {h["name"].lower(): h["value"] for h in payload.get("headers", [])}

        from_raw = headers.get("from", "")
        from_name, from_email = _parse_address(from_raw)
        from_domain = from_email.split("@")[-1].lower() if "@" in from_email else ""

        to_raw = headers.get("to", "")
        subject = _decode_header_value(headers.get("subject", "(no subject)"))
        date = headers.get("date", "")

        # Body extraction
        body_text = _extract_body(payload)

        # Attachment detection
        attachments = _find_attachments(payload)

        # Signal detection
        has_unsubscribe = _has_unsubscribe(headers, body_text)
        is_bulk = _is_bulk_sender(headers)
        reply_to_raw = headers.get("reply-to", "")
        reply_to_differs = _reply_to_differs(from_email, reply_to_raw)

        return EmailData(
            message_id=raw_message.get("id", ""),
            thread_id=raw_message.get("threadId", ""),
            from_addr=from_raw,
            from_name=from_name,
            from_email=from_email.lower(),
            from_domain=from_domain,
            to_addr=to_raw,
            subject=subject,
            date=date,
            snippet=raw_message.get("snippet", ""),
            body_text=body_text[:2000],
            labels=raw_message.get("labelIds", []),
            has_attachments=bool(attachments),
            attachment_names=attachments,
            has_unsubscribe_link=has_unsubscribe,
            is_bulk_sender=is_bulk,
            reply_to_differs=reply_to_differs,
            is_thread_reply=thread_message_count > 1,
        )


# ------------------------------------------------------------------
# Header helpers
# ------------------------------------------------------------------

def _parse_address(raw: str) -> Tuple[str, str]:
    name, addr = email.utils.parseaddr(raw)
    if not name and not addr:
        return "", raw.strip()
    if name:
        # Decode RFC 2047 encoded name
        name = _decode_header_value(name)
    return name, addr.strip().lower()


def _decode_header_value(value: str) -> str:
    try:
        parts = email.header.decode_header(value)
        decoded = []
        for part, charset in parts:
            if isinstance(part, bytes):
                try:
                    decoded.append(part.decode(charset or "utf-8", errors="replace"))
                except (LookupError, UnicodeDecodeError):
                    decoded.append(part.decode("utf-8", errors="replace"))
            else:
                decoded.append(part)
        return "".join(decoded)
    except Exception:
        return value


# ------------------------------------------------------------------
# Body extraction
# ------------------------------------------------------------------

def _extract_body(payload: dict) -> str:
    text = _walk_parts(payload)
    if not text:
        text = ""
    # Normalize whitespace
    text = re.sub(r"\r\n", "\n", text)
    text = re.sub(r"\n{3,}", "\n\n", text)
    return text.strip()


def _walk_parts(payload: dict) -> str:
    mime_type = payload.get("mimeType", "")

    if mime_type == "text/plain":
        return _decode_body_data(payload.get("body", {}).get("data", ""))

    if mime_type == "text/html":
        html = _decode_body_data(payload.get("body", {}).get("data", ""))
        return _strip_html(html)

    # Multipart: recurse, prefer plain text
    parts = payload.get("parts", [])
    plain_text = ""
    html_text = ""
    for part in parts:
        part_mime = part.get("mimeType", "")
        if part_mime == "text/plain":
            plain_text = _decode_body_data(part.get("body", {}).get("data", ""))
        elif part_mime == "text/html" and not plain_text:
            html_text = _strip_html(_decode_body_data(part.get("body", {}).get("data", "")))
        elif part_mime.startswith("multipart/"):
            sub = _walk_parts(part)
            if sub and not plain_text:
                plain_text = sub

    return plain_text or html_text


def _decode_body_data(data: str) -> str:
    if not data:
        return ""
    try:
        # Gmail uses URL-safe base64
        padded = data + "=" * (4 - len(data) % 4)
        decoded = base64.urlsafe_b64decode(padded)
        return decoded.decode("utf-8", errors="replace")
    except Exception:
        return ""


class _HTMLStripper(HTMLParser):
    def __init__(self) -> None:
        super().__init__()
        self._parts: List[str] = []
        self._skip_tags = {"script", "style", "head"}
        self._skip = False

    def handle_starttag(self, tag: str, attrs) -> None:
        if tag in self._skip_tags:
            self._skip = True
        if tag in ("br", "p", "div", "li", "tr"):
            self._parts.append("\n")

    def handle_endtag(self, tag: str) -> None:
        if tag in self._skip_tags:
            self._skip = False

    def handle_data(self, data: str) -> None:
        if not self._skip:
            self._parts.append(data)

    def get_text(self) -> str:
        return "".join(self._parts)


def _strip_html(html: str) -> str:
    try:
        stripper = _HTMLStripper()
        stripper.feed(html)
        return stripper.get_text()
    except Exception:
        # Fallback: crude tag removal
        return re.sub(r"<[^>]+>", " ", html)


# ------------------------------------------------------------------
# Attachment detection
# ------------------------------------------------------------------

def _find_attachments(payload: dict) -> List[str]:
    names: List[str] = []
    _collect_attachments(payload, names)
    return names


def _collect_attachments(part: dict, names: List[str]) -> None:
    body = part.get("body", {})
    filename = part.get("filename", "")
    if filename and body.get("attachmentId"):
        names.append(filename)
    for sub in part.get("parts", []):
        _collect_attachments(sub, names)


# ------------------------------------------------------------------
# Signal detection
# ------------------------------------------------------------------

_UNSUBSCRIBE_RE = re.compile(r"unsubscribe", re.IGNORECASE)


def _has_unsubscribe(headers: dict, body: str) -> bool:
    if "list-unsubscribe" in headers:
        return True
    return bool(_UNSUBSCRIBE_RE.search(body))


def _is_bulk_sender(headers: dict) -> bool:
    if "list-id" in headers:
        return True
    precedence = headers.get("precedence", "").lower()
    if precedence in ("bulk", "list", "junk"):
        return True
    x_mailer = headers.get("x-mailer", "").lower()
    # Common bulk mailer strings
    bulk_mailers = ("mailchimp", "sendgrid", "klaviyo", "constant contact", "marketo")
    return any(bm in x_mailer for bm in bulk_mailers)


def _reply_to_differs(from_email: str, reply_to_raw: str) -> bool:
    if not reply_to_raw:
        return False
    _, reply_addr = _parse_address(reply_to_raw)
    if not reply_addr:
        return False
    from_domain = from_email.split("@")[-1] if "@" in from_email else ""
    reply_domain = reply_addr.split("@")[-1] if "@" in reply_addr else ""
    return from_domain != reply_domain
