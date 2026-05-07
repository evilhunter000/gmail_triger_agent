from __future__ import annotations

import os
import random
import time
from typing import Dict, List, Optional, Tuple

from google.auth.transport.requests import Request
from google.oauth2.credentials import Credentials
from google_auth_oauthlib.flow import InstalledAppFlow
from googleapiclient.discovery import build
from googleapiclient.errors import HttpError

from config import Config

SCOPES = [
    "https://www.googleapis.com/auth/gmail.readonly",
    "https://www.googleapis.com/auth/gmail.modify",
]

# Labels the agent creates and manages
AGENT_LABEL_NAMES = {
    "important": "Auto/Important-Agent",
    "needs_review": "Auto/Needs-Review-Agent",
    "archived_promo": "Auto/Archived-Promo",
    "processed": "Auto/Processed-Agent",
}


class GmailClient:
    def __init__(self, config: Config) -> None:
        self._config = config
        self._service = _authenticate(config.google_credentials_file, config.token_file)
        self._label_cache: Dict[str, str] = {}  # name → id
        self._ensure_agent_labels()

    # ------------------------------------------------------------------
    # Authentication
    # ------------------------------------------------------------------

    # (handled by module-level _authenticate)

    # ------------------------------------------------------------------
    # Label management
    # ------------------------------------------------------------------

    def _ensure_agent_labels(self) -> None:
        for label_name in AGENT_LABEL_NAMES.values():
            self.get_or_create_label(label_name)

    def get_or_create_label(self, label_name: str) -> str:
        if label_name in self._label_cache:
            return self._label_cache[label_name]

        existing = self._call_with_backoff(
            self._service.users().labels().list(userId="me")
        )
        for lbl in existing.get("labels", []):
            self._label_cache[lbl["name"]] = lbl["id"]

        if label_name in self._label_cache:
            return self._label_cache[label_name]

        # Create the label
        body = {
            "name": label_name,
            "labelListVisibility": "labelShow",
            "messageListVisibility": "show",
        }
        created = self._call_with_backoff(
            self._service.users().labels().create(userId="me", body=body)
        )
        self._label_cache[label_name] = created["id"]
        return created["id"]

    def _resolve_label_ids(self, label_names: List[str]) -> List[str]:
        return [self.get_or_create_label(n) for n in label_names]

    # ------------------------------------------------------------------
    # Fetching emails
    # ------------------------------------------------------------------

    def get_inbox_messages(
        self,
        max_results: int = 50,
        history_id: Optional[str] = None,
    ) -> Tuple[List[str], str]:
        """Return (list_of_message_ids, new_history_id)."""
        if history_id:
            try:
                return self._fetch_incremental(history_id, max_results)
            except HttpError as exc:
                if exc.resp.status == 404:
                    # historyId expired (>7 days old) — fall back to full fetch
                    print(
                        "[gmail_client] historyId expired, falling back to full inbox fetch."
                    )
                else:
                    raise

        return self._fetch_full(max_results)

    def _fetch_incremental(
        self, history_id: str, max_results: int
    ) -> Tuple[List[str], str]:
        result = self._call_with_backoff(
            self._service.users()
            .history()
            .list(
                userId="me",
                startHistoryId=history_id,
                historyTypes=["messageAdded"],
                labelId="INBOX",
                maxResults=max_results,
            )
        )
        message_ids: List[str] = []
        for record in result.get("history", []):
            for added in record.get("messagesAdded", []):
                msg = added.get("message", {})
                # Only process messages still in INBOX
                if "INBOX" in msg.get("labelIds", []):
                    message_ids.append(msg["id"])

        new_history_id = result.get("historyId", history_id)
        return message_ids, new_history_id

    def _fetch_full(self, max_results: int) -> Tuple[List[str], str]:
        result = self._call_with_backoff(
            self._service.users()
            .messages()
            .list(
                userId="me",
                labelIds=["INBOX"],
                q="is:unread",
                maxResults=max_results,
            )
        )
        message_ids = [m["id"] for m in result.get("messages", [])]

        # Get current historyId from profile for next incremental fetch
        profile = self._call_with_backoff(
            self._service.users().getProfile(userId="me")
        )
        new_history_id = profile.get("historyId", "")
        return message_ids, new_history_id

    def get_message(self, message_id: str) -> dict:
        return self._call_with_backoff(
            self._service.users()
            .messages()
            .get(userId="me", id=message_id, format="full")
        )

    def get_thread_message_count(self, thread_id: str) -> int:
        try:
            thread = self._call_with_backoff(
                self._service.users().threads().get(userId="me", id=thread_id, format="minimal")
            )
            return len(thread.get("messages", []))
        except Exception:
            return 1

    # ------------------------------------------------------------------
    # Mutations
    # ------------------------------------------------------------------

    def apply_labels(self, message_id: str, label_names: List[str]) -> None:
        label_ids = self._resolve_label_ids(label_names)
        self._call_with_backoff(
            self._service.users()
            .messages()
            .modify(
                userId="me",
                id=message_id,
                body={"addLabelIds": label_ids},
            )
        )

    def archive_message(self, message_id: str) -> None:
        """Remove INBOX label — moves to All Mail (Gmail archive). Never deletes."""
        self._call_with_backoff(
            self._service.users()
            .messages()
            .modify(
                userId="me",
                id=message_id,
                body={"removeLabelIds": ["INBOX"]},
            )
        )

    def restore_archived(self, message_id: str) -> None:
        """Put email back in INBOX."""
        self._call_with_backoff(
            self._service.users()
            .messages()
            .modify(
                userId="me",
                id=message_id,
                body={"addLabelIds": ["INBOX"]},
            )
        )

    # ------------------------------------------------------------------
    # Rate-limit safe API caller
    # ------------------------------------------------------------------

    def _call_with_backoff(self, request, max_retries: int = 5):
        delay = 1.0
        last_exc: Optional[Exception] = None
        for attempt in range(max_retries + 1):
            try:
                return request.execute()
            except HttpError as exc:
                last_exc = exc
                if exc.resp.status in (429, 500, 503) and attempt < max_retries:
                    jitter = random.uniform(0, delay * 0.3)
                    time.sleep(min(delay + jitter, 32.0))
                    delay = min(delay * 2, 32.0)
                    continue  # retry
                raise
        raise last_exc  # exhausted retries


def _authenticate(credentials_file: str, token_file: str):
    creds: Optional[Credentials] = None

    if os.path.exists(token_file):
        creds = Credentials.from_authorized_user_file(token_file, SCOPES)

    if not creds or not creds.valid:
        if creds and creds.expired and creds.refresh_token:
            creds.refresh(Request())
        else:
            if not os.path.exists(credentials_file):
                raise FileNotFoundError(
                    f"credentials.json not found at '{credentials_file}'.\n"
                    "Follow the README setup instructions to download it from Google Cloud Console."
                )
            flow = InstalledAppFlow.from_client_secrets_file(credentials_file, SCOPES)
            creds = flow.run_local_server(port=0)

        with open(token_file, "w") as f:
            f.write(creds.to_json())

    return build("gmail", "v1", credentials=creds)
