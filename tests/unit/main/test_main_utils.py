"""Tests for pure utility functions in src/main.py."""

import importlib.util
import os
import sys

import httpx
import pytest
import respx

# Load src/main.py directly by path to avoid name collision with this package
_src_main_path = os.path.join(os.path.dirname(__file__), "../../../src/main.py")
_spec = importlib.util.spec_from_file_location("src_main", _src_main_path)
_src_main = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(_src_main)

_is_sensitive = _src_main._is_sensitive
_check_ollama = _src_main._check_ollama
_build_parser = _src_main._build_parser
_build_config_async = _src_main._build_config_async


# ── _is_sensitive ─────────────────────────────────────────────────────────────

def test_is_sensitive_password_field_type():
    assert _is_sensitive("password", "userpass") is True

def test_is_sensitive_password_in_field_name():
    assert _is_sensitive("text", "user_password") is True

def test_is_sensitive_passwd_in_field_name():
    assert _is_sensitive("text", "passwd") is True

def test_is_sensitive_pwd_in_field_name():
    assert _is_sensitive("text", "login_pwd") is True

def test_is_sensitive_cvv_in_field_name():
    assert _is_sensitive("text", "cvv") is True

def test_is_sensitive_ssn_in_field_name():
    assert _is_sensitive("text", "ssn_number") is True

def test_is_sensitive_pin_in_field_name():
    assert _is_sensitive("text", "bank_pin") is True

def test_is_sensitive_otp_in_field_name():
    assert _is_sensitive("text", "otp_code") is True

def test_is_sensitive_secret_in_field_name():
    assert _is_sensitive("text", "api_secret") is True

def test_is_sensitive_card_num_in_field_name():
    assert _is_sensitive("text", "card_number") is True

def test_not_sensitive_email_type():
    assert _is_sensitive("email", "user_email") is False

def test_not_sensitive_text_type_plain_name():
    assert _is_sensitive("text", "username") is False

def test_not_sensitive_tel_type():
    assert _is_sensitive("tel", "phone") is False

def test_not_sensitive_number_type():
    assert _is_sensitive("number", "age") is False

def test_is_sensitive_case_insensitive_name():
    # "Password" with capital P should still match
    assert _is_sensitive("text", "Password") is True

def test_is_sensitive_case_insensitive_cvv():
    assert _is_sensitive("text", "CVV") is True


# ── _check_ollama ─────────────────────────────────────────────────────────────

async def test_check_ollama_returns_true_when_server_responds_200():
    with respx.mock(base_url="http://localhost:11434") as router:
        router.get("/").respond(200, text="Ollama is running")
        result = await _check_ollama()
    assert result is True


async def test_check_ollama_returns_true_when_server_responds_404():
    # Any non-5xx is acceptable (< 500)
    with respx.mock(base_url="http://localhost:11434") as router:
        router.get("/").respond(404, text="not found")
        result = await _check_ollama()
    assert result is True


async def test_check_ollama_returns_false_when_server_responds_500():
    with respx.mock(base_url="http://localhost:11434") as router:
        router.get("/").respond(500, text="error")
        result = await _check_ollama()
    assert result is False


async def test_check_ollama_returns_false_on_connection_error():
    with respx.mock(base_url="http://localhost:11434") as router:
        router.get("/").mock(side_effect=httpx.ConnectError("refused"))
        result = await _check_ollama()
    assert result is False


async def test_check_ollama_returns_false_on_any_exception():
    with respx.mock(base_url="http://localhost:11434") as router:
        router.get("/").mock(side_effect=RuntimeError("unexpected"))
        result = await _check_ollama()
    assert result is False


# ── --mock --no-interactive without --intent ───────────────────────────────────

async def test_mock_no_interactive_without_intent_calls_parser_error():
    """--mock --no-interactive without --intent must call parser.error, not enter REPL."""
    parser = _build_parser()
    args = parser.parse_args(["--mock", "--no-interactive"])
    # parser.error() raises SystemExit in argparse
    with pytest.raises(SystemExit) as exc_info:
        await _build_config_async(args, parser)
    # argparse exits with code 2 on error
    assert exc_info.value.code == 2


async def test_mock_no_interactive_with_intent_succeeds():
    """--mock --no-interactive --intent TEXT must not raise."""
    parser = _build_parser()
    args = parser.parse_args(["--mock", "--no-interactive", "--intent", "test goal"])
    config = await _build_config_async(args, parser)
    assert config.mock is True
    assert config.no_interactive is True


# ── SSRF guard applied before page.goto ───────────────────────────────────────

def test_validate_url_blocks_aws_imds():
    """validate_url must raise ValueError for the AWS IMDS address."""
    from security.allowlist import validate_url
    with pytest.raises(ValueError):
        validate_url("http://169.254.169.254/latest/meta-data/")


def test_validate_url_blocks_localhost():
    """validate_url must raise ValueError for localhost."""
    from security.allowlist import validate_url
    with pytest.raises(ValueError):
        validate_url("http://localhost/admin")


def test_validate_url_passes_public_url():
    """validate_url must not raise for a normal public HTTPS URL."""
    from security.allowlist import validate_url
    validate_url("https://jsonplaceholder.typicode.com/posts")


# ── /provider ollama restores config on failure ──────────────────────────────

async def test_provider_ollama_failure_restores_previous_config():
    """/provider ollama with a failing health check must revert config to previous state."""
    from unittest.mock import patch

    SessionConfig = _src_main.SessionConfig
    _handle_command = _src_main._handle_command

    config = SessionConfig(provider="anthropic", model="claude-sonnet-4-6", api_key="sk-test")

    # Patch _check_ollama to simulate server not running
    with patch.object(_src_main, "_check_ollama", return_value=False):
        result = await _handle_command("/provider ollama", config)

    assert result is True  # REPL should continue
    assert config.provider == "anthropic"
    assert config.model == "claude-sonnet-4-6"
    assert config.api_key == "sk-test"
