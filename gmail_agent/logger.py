from __future__ import annotations

import json
import logging
import logging.handlers
from datetime import datetime, timezone
from typing import Optional

from actions import ActionResult
from classifier import ClassificationResult, LLMResult
from email_parser import EmailData
from rules import RuleResult


def _utcnow() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds").replace("+00:00", "Z")


class AuditLogger:
    def __init__(self, log_file: str = "gmail_agent_audit.jsonl", verbose: bool = False) -> None:
        self._verbose = verbose

        # JSONL rotating file handler
        self._file_logger = logging.getLogger("gmail_agent.audit")
        self._file_logger.setLevel(logging.DEBUG)
        self._file_logger.propagate = False

        if not self._file_logger.handlers:
            handler = logging.handlers.RotatingFileHandler(
                log_file,
                maxBytes=10 * 1024 * 1024,  # 10 MB
                backupCount=5,
                encoding="utf-8",
            )
            handler.setFormatter(logging.Formatter("%(message)s"))
            self._file_logger.addHandler(handler)

        # Console logger (INFO level by default, DEBUG if verbose)
        self._console_logger = logging.getLogger("gmail_agent.console")
        self._console_logger.setLevel(logging.DEBUG if verbose else logging.INFO)
        self._console_logger.propagate = False

        if not self._console_logger.handlers:
            console_handler = logging.StreamHandler()
            console_handler.setFormatter(logging.Formatter("[%(asctime)s] %(message)s", "%H:%M:%S"))
            self._console_logger.addHandler(console_handler)

    # ------------------------------------------------------------------
    # Core audit event
    # ------------------------------------------------------------------

    def log_classification(
        self,
        email: EmailData,
        rule_result: RuleResult,
        llm_result: Optional[LLMResult],
        final: ClassificationResult,
        action: ActionResult,
    ) -> None:
        record = {
            "timestamp": _utcnow(),
            "event": "email_classified",
            "message_id": email.message_id,
            "thread_id": email.thread_id,
            "from": email.from_email,
            "subject": email.subject,
            "rule_score": round(rule_result.score, 4),
            "rule_hint": rule_result.category_hint,
            "rule_signals": rule_result.signals_matched[:5],
            "used_llm": final.used_llm,
            "llm_confidence": round(llm_result.confidence, 4) if llm_result else None,
            "llm_category": llm_result.category if llm_result else None,
            "cache_hit": llm_result.cache_hit if llm_result else False,
            "final_category": final.category,
            "final_confidence": round(final.confidence, 4),
            "reason": final.reason,
            "suggested_action": final.suggested_action,
            "action_taken": action.action_taken,
            "labels_added": action.labels_added,
            "archived": action.archived,
            "dry_run": action.dry_run,
            "high_risk_domain": rule_result.high_risk_domain,
        }
        self._emit(record)

        # Console summary line
        cache_str = " [cache HIT]" if (llm_result and llm_result.cache_hit) else ""
        llm_str = f" → LLM:{final.llm_confidence:.2f}{cache_str}" if final.used_llm else ""
        action_str = action.action_taken.upper()
        self._console_logger.info(
            f"{final.category:<12} conf={final.confidence:.2f}  "
            f"rules={rule_result.score:+.2f}{llm_str}  "
            f"→ {action_str}  |  {email.from_email}  |  {email.subject[:50]}"
        )

        if self._verbose:
            self._console_logger.debug(
                f"  Signals: {', '.join(rule_result.signals_matched[:4])}\n"
                f"  Reason:  {final.reason[:120]}"
            )

    # ------------------------------------------------------------------
    # Poll lifecycle events
    # ------------------------------------------------------------------

    def log_poll_start(self, run_id: str, dry_run: bool) -> None:
        mode = "DRY-RUN" if dry_run else "LIVE"
        self._console_logger.info(f"--- Poll started | run={run_id} | mode={mode} ---")
        self._emit({"timestamp": _utcnow(), "event": "poll_start", "run_id": run_id, "dry_run": dry_run})

    def log_poll_end(self, run_id: str, processed: int, errors: int) -> None:
        self._console_logger.info(
            f"--- Poll complete | run={run_id} | processed={processed} | errors={errors} ---"
        )
        self._emit({
            "timestamp": _utcnow(),
            "event": "poll_end",
            "run_id": run_id,
            "processed": processed,
            "errors": errors,
        })

    # ------------------------------------------------------------------
    # Error logging
    # ------------------------------------------------------------------

    def log_error(self, context: str, error: Exception) -> None:
        self._console_logger.error(f"[ERROR] {context}: {error}")
        self._emit({
            "timestamp": _utcnow(),
            "event": "error",
            "context": context,
            "error": str(error),
        })

    # ------------------------------------------------------------------
    # Internal
    # ------------------------------------------------------------------

    def _emit(self, record: dict) -> None:
        self._file_logger.info(json.dumps(record, ensure_ascii=False))
