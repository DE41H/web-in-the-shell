"""Central defaults and utilities shared across the project."""
from __future__ import annotations

from datetime import datetime, UTC


def utcnow() -> datetime:
    """Return current UTC time as timezone-aware datetime."""
    return datetime.now(UTC)


def utcnow_iso() -> str:
    """Return current UTC time as ISO 8601 string."""
    return utcnow().isoformat()


# Rate limiting defaults (also in DispatchClient, referenced here for docs)
DEFAULT_RPS = 5.0
DEFAULT_BURST = 10
DEFAULT_MAX_RETRIES = 2

# LLM token limits
MAX_TOKENS_PLANNER = 512
MAX_TOKENS_EXECUTOR = 256
MAX_TOKENS_RECOVERY = 256

# Pipeline defaults
MAX_STATE_HISTORY = 10
MAX_CONVERSATION_HISTORY = 20  # cap on replayed ConvoStore messages

# Persistence
DEFAULT_DB_PATH_ENV = "WITS_DB_PATH"
