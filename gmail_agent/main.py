from __future__ import annotations

import argparse
import sys
import time
import uuid
from datetime import datetime, timezone

from actions import ActionExecutor
from classifier import Classifier, ClassificationResult, combine_scores
from config import Config, load_config
from email_parser import EmailParser
from gmail_client import GmailClient
from logger import AuditLogger
from notifier import Notifier
from rules import RuleEngine
from storage import AuditRecord, Storage


# ------------------------------------------------------------------
# Per-email processing pipeline
# ------------------------------------------------------------------

def process_email(
    message_id: str,
    gmail: GmailClient,
    parser: EmailParser,
    rules: RuleEngine,
    classifier: Classifier,
    executor: ActionExecutor,
    notifier: Notifier,
    storage: Storage,
    audit: AuditLogger,
    config: Config,
) -> bool:
    """Process a single email. Returns True on success, False on error."""
    try:
        # Deduplication
        if storage.is_processed(message_id):
            return True

        # Fetch full message
        raw = gmail.get_message(message_id)
        thread_count = gmail.get_thread_message_count(raw.get("threadId", message_id))

        # Parse
        email = parser.parse(raw, thread_message_count=thread_count)

        # Rule-based scoring
        rule_result = rules.score(email)

        # LLM classification (only when rules are indecisive)
        llm_result = None
        if not rule_result.is_decisive:
            try:
                llm_result = classifier.classify(email)
            except RuntimeError as exc:
                audit.log_error(f"LLM classify {message_id}", exc)
                # Fall through — combine_scores handles None llm_result conservatively

        # Combine scores → final classification
        final = combine_scores(
            rule_result,
            llm_result,
            important_threshold=config.important_confidence_threshold,
            spam_threshold=config.spam_confidence_threshold,
        )

        # Execute Gmail action
        action = executor.execute(email, final, rule_result)

        # Send notification for Important emails
        if final.category == "Important" and final.confidence >= config.important_confidence_threshold:
            notifier.notify_important(email, final)
        elif (
            final.category == "Normal"
            and rule_result.high_risk_domain
            and final.confidence >= config.important_confidence_threshold - 0.10
        ):
            notifier.notify_uncertain(email, final)

        # Persist to SQLite
        now = datetime.now(timezone.utc).isoformat(timespec="seconds").replace("+00:00", "Z")
        record = AuditRecord(
            message_id=email.message_id,
            thread_id=email.thread_id,
            processed_at=now,
            from_addr=email.from_email,
            subject=email.subject,
            category=final.category,
            confidence=final.confidence,
            rule_score=rule_result.score,
            llm_confidence=final.llm_confidence,
            reason=final.reason,
            action_taken=action.action_taken,
            labels_added=action.labels_added,
            archived=action.archived,
            dry_run=action.dry_run,
        )
        storage.record_email(record)

        # Audit log
        audit.log_classification(email, rule_result, llm_result, final, action)

        return True

    except Exception as exc:
        audit.log_error(f"process_email {message_id}", exc)
        return False


# ------------------------------------------------------------------
# Single poll cycle
# ------------------------------------------------------------------

def run_poll(
    gmail: GmailClient,
    parser: EmailParser,
    rules: RuleEngine,
    classifier: Classifier,
    executor: ActionExecutor,
    notifier: Notifier,
    storage: Storage,
    audit: AuditLogger,
    config: Config,
) -> None:
    run_id = str(uuid.uuid4())[:8]
    audit.log_poll_start(run_id, config.dry_run)

    history_id = storage.get_history_id()
    message_ids, new_history_id = gmail.get_inbox_messages(
        max_results=config.max_emails_per_run,
        history_id=history_id,
    )

    processed = 0
    errors = 0
    for mid in message_ids:
        ok = process_email(
            mid, gmail, parser, rules, classifier, executor, notifier, storage, audit, config
        )
        if ok:
            processed += 1
        else:
            errors += 1

    if new_history_id:
        storage.set_history_id(new_history_id)

    audit.log_poll_end(run_id, processed, errors)


# ------------------------------------------------------------------
# Daily digest check
# ------------------------------------------------------------------

def _check_daily_digest(config: Config, storage: Storage, notifier: Notifier) -> None:
    if not config.daily_digest_enabled:
        return

    now = datetime.now(timezone.utc)
    if now.hour < config.daily_digest_hour:
        return

    today_str = now.strftime("%Y-%m-%d")
    last_sent = storage.get_setting("last_digest_sent")
    if last_sent == today_str:
        return

    stats = storage.get_stats_today()
    archived = storage.get_archived_today()
    notifier.send_daily_digest(stats, archived)
    storage.set_setting("last_digest_sent", today_str)


# ------------------------------------------------------------------
# Restore archived email
# ------------------------------------------------------------------

def restore_email(message_id: str, executor: ActionExecutor, storage: Storage) -> None:
    archived = storage.was_archived(message_id)
    if archived is None:
        print(f"[restore] Message ID '{message_id}' not found in audit records.")
        print("  The email may still be restorable via Gmail — attempting anyway.")
    elif not archived:
        print(f"[restore] Message '{message_id}' was not archived by this agent.")
        print("  Attempting restore anyway in case it was manually archived.")
    executor.restore(message_id)


# ------------------------------------------------------------------
# Entry points
# ------------------------------------------------------------------

def run_once(config: Config) -> None:
    gmail, parser, rules, classifier, executor, notifier, storage, audit = _init_components(config)
    run_poll(gmail, parser, rules, classifier, executor, notifier, storage, audit, config)
    storage.close()


def run_loop(config: Config) -> None:
    gmail, parser, rules, classifier, executor, notifier, storage, audit = _init_components(config)

    print(f"[main] Polling every {config.poll_interval_seconds}s. Press Ctrl+C to stop.")
    try:
        while True:
            run_poll(gmail, parser, rules, classifier, executor, notifier, storage, audit, config)
            _check_daily_digest(config, storage, notifier)
            time.sleep(config.poll_interval_seconds)
    except KeyboardInterrupt:
        print("\n[main] Stopped by user.")
    finally:
        storage.close()


def _init_components(config: Config):
    gmail = GmailClient(config)
    parser = EmailParser()
    rules = RuleEngine(config)
    classifier = Classifier(config)
    storage = Storage(config.db_path)
    executor = ActionExecutor(config, gmail)
    notifier = Notifier(config)
    audit = AuditLogger(config.log_file)
    return gmail, parser, rules, classifier, executor, notifier, storage, audit


# ------------------------------------------------------------------
# CLI
# ------------------------------------------------------------------

def main() -> None:
    arg_parser = argparse.ArgumentParser(
        description="Gmail Triage Agent — intelligently classifies and triages your inbox.",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
Examples:
  python main.py --dry-run --once --verbose          # Test without modifying Gmail
  python main.py --dry-run                           # Continuous dry-run
  python main.py                                     # Live mode (set DRY_RUN=false in .env first)
  python main.py --restore <message_id>              # Restore an archived email to inbox
  python main.py --config .env.work                  # Run with a second Gmail account
  python main.py --config .env.personal --dry-run    # Dry-run a specific account
""",
    )
    arg_parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Classify emails but make no Gmail changes (overrides .env DRY_RUN setting)",
    )
    arg_parser.add_argument(
        "--once",
        action="store_true",
        help="Run one polling cycle and exit (useful for testing)",
    )
    arg_parser.add_argument(
        "--verbose",
        action="store_true",
        help="Print detailed classification output including signals and reasons",
    )
    arg_parser.add_argument(
        "--restore",
        metavar="MESSAGE_ID",
        help="Restore a previously archived email back to inbox",
    )
    arg_parser.add_argument(
        "--config",
        metavar="ENV_FILE",
        default=".env",
        help="Path to a .env config file (default: .env). Use this to run multiple accounts.",
    )
    args = arg_parser.parse_args()

    try:
        config = load_config(env_file=args.config)
    except ValueError as exc:
        print(f"[config] Error: {exc}")
        sys.exit(1)

    if args.dry_run:
        config.dry_run = True

    if config.dry_run:
        print("=" * 60)
        print("  RUNNING IN DRY-RUN MODE")
        print("  No changes will be made to your Gmail account.")
        print("  Set DRY_RUN=false in .env to enable real actions.")
        print("=" * 60)

    if args.restore:
        gmail, _, _, _, executor, _, storage, _ = _init_components(config)
        restore_email(args.restore, executor, storage)
        storage.close()
        return

    # Override verbose setting in logger
    if args.verbose:
        gmail, parser, rules, classifier, executor, notifier, storage, audit = _init_components(config)
        audit._verbose = True
        audit._console_logger.setLevel(10)  # DEBUG
        if args.once:
            run_poll(gmail, parser, rules, classifier, executor, notifier, storage, audit, config)
            storage.close()
        else:
            print(f"[main] Polling every {config.poll_interval_seconds}s. Press Ctrl+C to stop.")
            try:
                while True:
                    run_poll(gmail, parser, rules, classifier, executor, notifier, storage, audit, config)
                    _check_daily_digest(config, storage, notifier)
                    time.sleep(config.poll_interval_seconds)
            except KeyboardInterrupt:
                print("\n[main] Stopped by user.")
            finally:
                storage.close()
        return

    if args.once:
        run_once(config)
    else:
        run_loop(config)


if __name__ == "__main__":
    main()
