from __future__ import annotations

import json
import sqlite3
import threading
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import List, Optional


def _utcnow() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds").replace("+00:00", "Z")


@dataclass
class AuditRecord:
    message_id: str
    thread_id: str
    processed_at: str
    from_addr: str
    subject: str
    category: str            # 'Important' | 'Promotional' | 'Normal'
    confidence: float
    rule_score: Optional[float]
    llm_confidence: Optional[float]
    reason: str
    action_taken: str        # 'notified' | 'archived' | 'labeled' | 'skipped'
    labels_added: List[str]
    archived: bool
    dry_run: bool


_CREATE_PROCESSED = """
CREATE TABLE IF NOT EXISTS processed_emails (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    message_id TEXT UNIQUE NOT NULL,
    thread_id TEXT,
    processed_at TEXT NOT NULL,
    from_addr TEXT,
    subject TEXT,
    category TEXT,
    confidence REAL,
    rule_score REAL,
    llm_confidence REAL,
    reason TEXT,
    action_taken TEXT,
    labels_added TEXT,
    archived INTEGER NOT NULL DEFAULT 0,
    dry_run INTEGER NOT NULL DEFAULT 0
)
"""

_CREATE_SETTINGS = """
CREATE TABLE IF NOT EXISTS settings (
    key TEXT PRIMARY KEY,
    value TEXT NOT NULL,
    updated_at TEXT NOT NULL
)
"""

_CREATE_IDX_PROCESSED_AT = "CREATE INDEX IF NOT EXISTS idx_processed_at ON processed_emails(processed_at)"
_CREATE_IDX_CATEGORY = "CREATE INDEX IF NOT EXISTS idx_category ON processed_emails(category)"
_CREATE_IDX_ARCHIVED = "CREATE INDEX IF NOT EXISTS idx_archived ON processed_emails(archived)"


class Storage:
    def __init__(self, db_path: str = "gmail_agent.db") -> None:
        self._db_path = db_path
        self._lock = threading.Lock()
        self._conn = sqlite3.connect(db_path, check_same_thread=False)
        self._conn.row_factory = sqlite3.Row
        self._init_schema()

    def _init_schema(self) -> None:
        with self._lock:
            cur = self._conn.cursor()
            cur.execute(_CREATE_PROCESSED)
            cur.execute(_CREATE_SETTINGS)
            cur.execute(_CREATE_IDX_PROCESSED_AT)
            cur.execute(_CREATE_IDX_CATEGORY)
            cur.execute(_CREATE_IDX_ARCHIVED)
            self._conn.commit()

    # ------------------------------------------------------------------
    # Deduplication
    # ------------------------------------------------------------------

    def is_processed(self, message_id: str) -> bool:
        with self._lock:
            cur = self._conn.execute(
                "SELECT 1 FROM processed_emails WHERE message_id = ? LIMIT 1",
                (message_id,),
            )
            return cur.fetchone() is not None

    def was_archived(self, message_id: str) -> Optional[bool]:
        """Returns True/False if message is in records, None if not found."""
        with self._lock:
            cur = self._conn.execute(
                "SELECT archived FROM processed_emails WHERE message_id = ?",
                (message_id,),
            )
            row = cur.fetchone()
            return bool(row["archived"]) if row else None

    # ------------------------------------------------------------------
    # Audit record persistence
    # ------------------------------------------------------------------

    def record_email(self, record: AuditRecord) -> None:
        with self._lock:
            self._conn.execute(
                """
                INSERT OR IGNORE INTO processed_emails
                    (message_id, thread_id, processed_at, from_addr, subject,
                     category, confidence, rule_score, llm_confidence, reason,
                     action_taken, labels_added, archived, dry_run)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    record.message_id,
                    record.thread_id,
                    record.processed_at,
                    record.from_addr,
                    record.subject,
                    record.category,
                    record.confidence,
                    record.rule_score,
                    record.llm_confidence,
                    record.reason,
                    record.action_taken,
                    json.dumps(record.labels_added),
                    1 if record.archived else 0,
                    1 if record.dry_run else 0,
                ),
            )
            self._conn.commit()

    # ------------------------------------------------------------------
    # Gmail historyId (incremental polling state)
    # ------------------------------------------------------------------

    def get_history_id(self) -> Optional[str]:
        return self.get_setting("gmail_history_id")

    def set_history_id(self, history_id: str) -> None:
        self.set_setting("gmail_history_id", history_id)

    # ------------------------------------------------------------------
    # Daily digest queries
    # ------------------------------------------------------------------

    def get_archived_today(self) -> List[AuditRecord]:
        today = datetime.now(timezone.utc).strftime("%Y-%m-%d")
        with self._lock:
            rows = self._conn.execute(
                "SELECT * FROM processed_emails WHERE archived = 1 AND processed_at LIKE ? ORDER BY processed_at DESC LIMIT 50",
                (f"{today}%",),
            ).fetchall()
        return [_row_to_record(r) for r in rows]

    def get_stats_today(self) -> dict:
        today = datetime.now(timezone.utc).strftime("%Y-%m-%d")
        with self._lock:
            cur = self._conn.execute(
                """
                SELECT
                    COUNT(*) as total,
                    SUM(CASE WHEN category = 'Important' THEN 1 ELSE 0 END) as important,
                    SUM(CASE WHEN category = 'Promotional' THEN 1 ELSE 0 END) as promotional,
                    SUM(CASE WHEN archived = 1 THEN 1 ELSE 0 END) as archived,
                    SUM(CASE WHEN category = 'Normal' THEN 1 ELSE 0 END) as normal
                FROM processed_emails
                WHERE processed_at LIKE ?
                """,
                (f"{today}%",),
            )
            row = cur.fetchone()
        return dict(row) if row else {}

    # ------------------------------------------------------------------
    # Generic key-value settings
    # ------------------------------------------------------------------

    def get_setting(self, key: str) -> Optional[str]:
        with self._lock:
            cur = self._conn.execute(
                "SELECT value FROM settings WHERE key = ?", (key,)
            )
            row = cur.fetchone()
            return row["value"] if row else None

    def set_setting(self, key: str, value: str) -> None:
        with self._lock:
            self._conn.execute(
                "INSERT OR REPLACE INTO settings (key, value, updated_at) VALUES (?, ?, ?)",
                (key, value, _utcnow()),
            )
            self._conn.commit()

    def close(self) -> None:
        self._conn.close()


def _row_to_record(row: sqlite3.Row) -> AuditRecord:
    labels_raw = row["labels_added"]
    try:
        labels = json.loads(labels_raw) if labels_raw else []
    except (json.JSONDecodeError, TypeError):
        labels = []
    return AuditRecord(
        message_id=row["message_id"],
        thread_id=row["thread_id"] or "",
        processed_at=row["processed_at"],
        from_addr=row["from_addr"] or "",
        subject=row["subject"] or "",
        category=row["category"] or "Normal",
        confidence=row["confidence"] or 0.0,
        rule_score=row["rule_score"],
        llm_confidence=row["llm_confidence"],
        reason=row["reason"] or "",
        action_taken=row["action_taken"] or "skipped",
        labels_added=labels,
        archived=bool(row["archived"]),
        dry_run=bool(row["dry_run"]),
    )
