from __future__ import annotations

import os
from dataclasses import dataclass
from typing import List, Optional

from dotenv import load_dotenv

_env_loaded = False


def _load_env(env_file: str = ".env") -> None:
    global _env_loaded
    load_dotenv(dotenv_path=env_file, override=True)
    _env_loaded = True


def _get(key: str, default: str = "") -> str:
    return os.environ.get(key, default).strip()


def _get_bool(key: str, default: bool = False) -> bool:
    val = _get(key, str(default)).lower()
    return val in ("1", "true", "yes")


def _get_int(key: str, default: int = 0) -> int:
    try:
        return int(_get(key, str(default)))
    except ValueError:
        return default


def _get_float(key: str, default: float = 0.0) -> float:
    try:
        return float(_get(key, str(default)))
    except ValueError:
        return default


def _get_list(key: str) -> List[str]:
    raw = _get(key, "")
    if not raw:
        return []
    return [item.strip().lower() for item in raw.split(",") if item.strip()]


@dataclass
class Config:
    # Gmail OAuth
    google_credentials_file: str
    token_file: str

    # Agent behavior
    dry_run: bool
    poll_interval_seconds: int
    max_emails_per_run: int

    # Claude API
    anthropic_api_key: str
    claude_model: str

    # Confidence thresholds
    important_confidence_threshold: float
    spam_confidence_threshold: float

    # Optional notifications
    telegram_bot_token: Optional[str]
    telegram_chat_id: Optional[str]

    # Safety lists
    whitelist_domains: List[str]
    blacklist_domains: List[str]
    whitelist_senders: List[str]

    # Daily digest
    daily_digest_enabled: bool
    daily_digest_hour: int

    # Storage
    db_path: str
    log_file: str


def load_config(env_file: str = ".env") -> Config:
    _load_env(env_file)
    anthropic_key = _get("ANTHROPIC_API_KEY")
    if not anthropic_key or anthropic_key.startswith("sk-ant-..."):
        raise ValueError(
            "ANTHROPIC_API_KEY is not set in .env. "
            "Get your key at https://console.anthropic.com/"
        )

    credentials_file = _get("GOOGLE_CREDENTIALS_FILE", "credentials.json")

    important_threshold = _get_float("IMPORTANT_CONFIDENCE_THRESHOLD", 0.75)
    spam_threshold = _get_float("SPAM_CONFIDENCE_THRESHOLD", 0.85)

    if not (0.5 < important_threshold < 1.0):
        raise ValueError("IMPORTANT_CONFIDENCE_THRESHOLD must be between 0.5 and 1.0")
    if not (0.5 < spam_threshold < 1.0):
        raise ValueError("SPAM_CONFIDENCE_THRESHOLD must be between 0.5 and 1.0")
    if spam_threshold <= important_threshold:
        raise ValueError(
            "SPAM_CONFIDENCE_THRESHOLD must be higher than IMPORTANT_CONFIDENCE_THRESHOLD "
            "(spam archiving should require more confidence than importance notification)"
        )

    telegram_token = _get("TELEGRAM_BOT_TOKEN") or None
    telegram_chat = _get("TELEGRAM_CHAT_ID") or None

    return Config(
        google_credentials_file=credentials_file,
        token_file=_get("TOKEN_FILE", "token.json"),
        dry_run=_get_bool("DRY_RUN", True),
        poll_interval_seconds=_get_int("POLL_INTERVAL_SECONDS", 60),
        max_emails_per_run=_get_int("MAX_EMAILS_PER_RUN", 50),
        anthropic_api_key=anthropic_key,
        claude_model=_get("CLAUDE_MODEL", "claude-sonnet-4-6"),
        important_confidence_threshold=important_threshold,
        spam_confidence_threshold=spam_threshold,
        telegram_bot_token=telegram_token,
        telegram_chat_id=telegram_chat,
        whitelist_domains=_get_list("WHITELIST_DOMAINS"),
        blacklist_domains=_get_list("BLACKLIST_DOMAINS"),
        whitelist_senders=_get_list("WHITELIST_SENDERS"),
        daily_digest_enabled=_get_bool("DAILY_DIGEST_ENABLED", True),
        daily_digest_hour=_get_int("DAILY_DIGEST_HOUR", 18),
        db_path=_get("DB_PATH", "gmail_agent.db"),
        log_file=_get("LOG_FILE", "gmail_agent_audit.jsonl"),
    )
