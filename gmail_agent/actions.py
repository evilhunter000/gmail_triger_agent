from __future__ import annotations

from dataclasses import dataclass
from typing import List

from classifier import ClassificationResult
from config import Config
from email_parser import EmailData
from gmail_client import AGENT_LABEL_NAMES, GmailClient
from rules import RuleResult


@dataclass
class ActionResult:
    action_taken: str       # 'notified' | 'archived' | 'labeled_normal' | 'labeled_needs_review' | 'skipped'
    labels_added: List[str]
    archived: bool
    dry_run: bool


class ActionExecutor:
    def __init__(self, config: Config, gmail: GmailClient) -> None:
        self._config = config
        self._gmail = gmail

    def execute(
        self,
        email: EmailData,
        classification: ClassificationResult,
        rule_result: RuleResult,
    ) -> ActionResult:
        dry_run = self._config.dry_run
        category = classification.category
        confidence = classification.confidence

        if category == "Important" and confidence >= self._config.important_confidence_threshold:
            return self._handle_important(email, confidence, dry_run)

        if category == "Promotional" and self._should_archive(classification, rule_result):
            return self._handle_promotional(email, confidence, dry_run)

        # Normal or below threshold → leave in inbox, label appropriately
        return self._handle_normal(email, confidence, dry_run)

    # ------------------------------------------------------------------
    # Per-category handlers
    # ------------------------------------------------------------------

    def _handle_important(
        self, email: EmailData, confidence: float, dry_run: bool
    ) -> ActionResult:
        labels = [
            AGENT_LABEL_NAMES["important"],
            AGENT_LABEL_NAMES["processed"],
        ]
        if dry_run:
            _print_dry_run("notify (Important)", email, confidence, labels)
        else:
            self._gmail.apply_labels(email.message_id, labels)
        return ActionResult(
            action_taken="notified",
            labels_added=labels,
            archived=False,
            dry_run=dry_run,
        )

    def _handle_promotional(
        self, email: EmailData, confidence: float, dry_run: bool
    ) -> ActionResult:
        labels = [
            AGENT_LABEL_NAMES["archived_promo"],
            AGENT_LABEL_NAMES["processed"],
        ]
        if dry_run:
            _print_dry_run("archive (Promotional)", email, confidence, labels)
        else:
            self._gmail.apply_labels(email.message_id, labels)
            self._gmail.archive_message(email.message_id)
        return ActionResult(
            action_taken="archived",
            labels_added=labels,
            archived=not dry_run,  # only truly archived if not dry-run
            dry_run=dry_run,
        )

    def _handle_normal(
        self, email: EmailData, confidence: float, dry_run: bool
    ) -> ActionResult:
        # Use needs_review label if confidence is meaningfully below threshold,
        # otherwise just mark as processed
        if confidence < self._config.important_confidence_threshold - 0.10:
            label_key = "needs_review"
            action = "labeled_needs_review"
        else:
            label_key = "processed"
            action = "labeled_normal"

        labels = [AGENT_LABEL_NAMES[label_key], AGENT_LABEL_NAMES["processed"]]
        labels = list(dict.fromkeys(labels))  # dedupe

        if dry_run:
            _print_dry_run(f"label as Normal ({label_key})", email, confidence, labels)
        else:
            self._gmail.apply_labels(email.message_id, labels)
        return ActionResult(
            action_taken=action,
            labels_added=labels,
            archived=False,
            dry_run=dry_run,
        )

    # ------------------------------------------------------------------
    # Archive safety gate
    # ------------------------------------------------------------------

    def _should_archive(
        self,
        classification: ClassificationResult,
        rule_result: RuleResult,
    ) -> bool:
        """
        Returns True only when ALL safety conditions are satisfied:
        1. Category is Promotional
        2. Confidence >= spam_confidence_threshold
        3. High-risk domains require even higher confidence (>= 0.90)
        """
        if classification.category != "Promotional":
            return False
        if classification.confidence < self._config.spam_confidence_threshold:
            return False
        if rule_result.high_risk_domain and classification.confidence < 0.90:
            return False
        return True

    # ------------------------------------------------------------------
    # Restore an archived email
    # ------------------------------------------------------------------

    def restore(self, message_id: str) -> None:
        """Restore a previously archived email back to INBOX."""
        self._gmail.restore_archived(message_id)
        print(f"[actions] Restored message {message_id} to inbox.")


# ------------------------------------------------------------------
# Dry-run output
# ------------------------------------------------------------------

def _print_dry_run(action: str, email: EmailData, confidence: float, labels: List[str]) -> None:
    label_str = ", ".join(labels)
    print(
        f"  DRY RUN: Would {action} | "
        f"from={email.from_email} | "
        f"subject='{email.subject[:60]}' | "
        f"conf={confidence:.2f} | "
        f"labels=[{label_str}]"
    )
