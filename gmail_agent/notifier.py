from __future__ import annotations

import json
import urllib.error
import urllib.request
from typing import List, Optional

from classifier import ClassificationResult
from config import Config
from email_parser import EmailData
from storage import AuditRecord

try:
    from plyer import notification as _plyer_notification

    _PLYER_AVAILABLE = True
except ImportError:
    _PLYER_AVAILABLE = False
    print(
        "[notifier] Warning: 'plyer' not installed. Desktop notifications disabled. "
        "Run: pip install plyer"
    )


class Notifier:
    def __init__(self, config: Config) -> None:
        self._config = config
        self._telegram_enabled = bool(
            config.telegram_bot_token and config.telegram_chat_id
        )

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    def notify_important(
        self,
        email: EmailData,
        classification: ClassificationResult,
    ) -> None:
        priority = "HIGH" if classification.confidence >= 0.88 else "MEDIUM"
        title = f"Important Email ({priority})"
        body = (
            f"From: {email.from_addr}\n"
            f"Subject: {email.subject}\n"
            f"Why: {classification.reason[:120]}\n"
            f"Action: {classification.suggested_action}"
        )
        self._send_desktop(title, body)
        self._send_telegram(_format_telegram_important(email, classification, priority))
        _print_console_notification(title, body)

    def notify_uncertain(
        self,
        email: EmailData,
        classification: ClassificationResult,
    ) -> None:
        """Notify only for uncertain emails from high-risk domains or with urgent signals."""
        title = "Email Needs Review"
        body = (
            f"From: {email.from_addr}\n"
            f"Subject: {email.subject}\n"
            f"Note: {classification.reason[:120]}"
        )
        self._send_desktop(title, body)
        self._send_telegram(_format_telegram_uncertain(email, classification))
        _print_console_notification(title, body)

    def send_daily_digest(
        self,
        stats: dict,
        archived_emails: List[AuditRecord],
    ) -> None:
        total = stats.get("total", 0)
        important = stats.get("important", 0)
        promotional = stats.get("promotional", 0)
        normal = stats.get("normal", 0)
        archived = stats.get("archived", 0)

        title = "Gmail Agent — Daily Digest"
        body = (
            f"Processed today: {total}\n"
            f"  Important: {important}  |  Promotional: {promotional}  |  Normal: {normal}\n"
            f"  Archived: {archived}\n"
        )
        if archived_emails:
            recent = archived_emails[:5]
            body += "Recently archived:\n"
            for rec in recent:
                body += f"  • {rec.from_addr}: {rec.subject[:50]}\n"

        self._send_desktop(title, body)
        self._send_telegram(_format_telegram_digest(stats, archived_emails))
        _print_console_notification(title, body)

    # ------------------------------------------------------------------
    # Delivery methods
    # ------------------------------------------------------------------

    def _send_desktop(self, title: str, message: str) -> None:
        if not _PLYER_AVAILABLE:
            return
        try:
            _plyer_notification.notify(
                title=title,
                message=message[:250],
                app_name="Gmail Agent",
                timeout=10,
            )
        except Exception as exc:
            print(f"[notifier] Desktop notification failed: {exc}")

    def _send_telegram(self, message: str) -> None:
        if not self._telegram_enabled:
            return
        try:
            url = f"https://api.telegram.org/bot{self._config.telegram_bot_token}/sendMessage"
            payload = json.dumps(
                {
                    "chat_id": self._config.telegram_chat_id,
                    "text": message[:4096],
                    "parse_mode": "Markdown",
                }
            ).encode("utf-8")
            req = urllib.request.Request(
                url,
                data=payload,
                headers={"Content-Type": "application/json"},
                method="POST",
            )
            with urllib.request.urlopen(req, timeout=10):
                pass
        except (urllib.error.URLError, urllib.error.HTTPError, OSError) as exc:
            print(f"[notifier] Telegram notification failed: {exc}")


# ------------------------------------------------------------------
# Formatting helpers
# ------------------------------------------------------------------

def _format_telegram_important(
    email: EmailData,
    classification: ClassificationResult,
    priority: str,
) -> str:
    return (
        f"*Important Email ({priority})*\n"
        f"*From:* `{email.from_email}`\n"
        f"*Subject:* {_esc(email.subject)}\n"
        f"*Why:* {_esc(classification.reason[:200])}\n"
        f"*Suggested action:* {classification.suggested_action}"
    )


def _format_telegram_uncertain(
    email: EmailData,
    classification: ClassificationResult,
) -> str:
    return (
        f"*Email Needs Review*\n"
        f"*From:* `{email.from_email}`\n"
        f"*Subject:* {_esc(email.subject)}\n"
        f"*Note:* {_esc(classification.reason[:200])}"
    )


def _format_telegram_digest(stats: dict, archived: List[AuditRecord]) -> str:
    lines = [
        "*Gmail Agent — Daily Digest*",
        f"Processed: {stats.get('total', 0)} | Important: {stats.get('important', 0)} | Archived: {stats.get('archived', 0)}",
    ]
    if archived:
        lines.append("\nRecently archived:")
        for rec in archived[:5]:
            lines.append(f"• `{rec.from_addr}` — {_esc(rec.subject[:50])}")
    return "\n".join(lines)


def _print_console_notification(title: str, body: str) -> None:
    print(f"\n{'='*60}")
    print(f"  {title}")
    print(f"{'='*60}")
    for line in body.strip().splitlines():
        print(f"  {line}")
    print(f"{'='*60}\n")


def _esc(text: str) -> str:
    """Escape Telegram Markdown special characters."""
    for char in r"\_*[]()~`>#+-=|{}.!":
        text = text.replace(char, f"\\{char}")
    return text
