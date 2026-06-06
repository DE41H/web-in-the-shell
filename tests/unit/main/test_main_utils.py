"""Tests for pure utility functions in src/main.py."""

import importlib.util
import os

import httpx
import respx

# Load src/main.py directly by path to avoid name collision with this package
_src_main_path = os.path.join(os.path.dirname(__file__), "../../../src/main.py")
_spec = importlib.util.spec_from_file_location("src_main", _src_main_path)
_src_main = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(_src_main)

_is_sensitive = _src_main._is_sensitive
_check_ollama = _src_main._check_ollama


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
