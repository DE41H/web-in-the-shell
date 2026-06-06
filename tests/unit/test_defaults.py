"""Tests for src/defaults.py — utcnow, utcnow_iso, and module constants."""
from __future__ import annotations

from datetime import datetime

import defaults


def test_utcnow_returns_timezone_aware_datetime():
    result = defaults.utcnow()
    assert isinstance(result, datetime)
    assert result.tzinfo is not None


def test_utcnow_iso_returns_parseable_string():
    result = defaults.utcnow_iso()
    parsed = datetime.fromisoformat(result)
    assert parsed.tzinfo is not None


def test_utcnow_iso_ends_with_utc_offset():
    result = defaults.utcnow_iso()
    assert result.endswith("+00:00") or "Z" in result


def test_default_rps_is_positive():
    assert defaults.DEFAULT_RPS > 0


def test_default_burst_is_positive():
    assert defaults.DEFAULT_BURST > 0


def test_max_tokens_planner_is_positive():
    assert defaults.MAX_TOKENS_PLANNER > 0


def test_max_tokens_executor_is_positive():
    assert defaults.MAX_TOKENS_EXECUTOR > 0


def test_max_tokens_recovery_is_positive():
    assert defaults.MAX_TOKENS_RECOVERY > 0


def test_max_state_history_is_positive():
    assert defaults.MAX_STATE_HISTORY > 0


def test_max_conversation_history_is_positive():
    assert defaults.MAX_CONVERSATION_HISTORY > 0


def test_default_db_path_env_is_non_empty_string():
    assert isinstance(defaults.DEFAULT_DB_PATH_ENV, str)
    assert len(defaults.DEFAULT_DB_PATH_ENV) > 0
