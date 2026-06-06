from security.redact import redact


# ---- bearer ----

def test_bearer_token_redacted():
    assert redact("Bearer abc123def456ghi789") == "Bearer [REDACTED]"


def test_short_bearer_unchanged():
    assert redact("Authorization: Bearer short") == "Authorization: Bearer short"


# ---- JWT ----

def test_jwt_shaped_redacted():
    assert redact("eyJabc.def-ghi.jkl_mno") == "[JWT REDACTED]"


def test_jwt_as_bearer_value_redacted_via_jwt_step():
    assert redact("Bearer eyJabc.def-ghi.jkl_mno") == "Bearer [JWT REDACTED]"


# ---- key=value ----

def test_long_key_value_redacted():
    assert redact("api_key=1234567890abcdef") == "api_key=[REDACTED]"


def test_short_key_value_unchanged():
    assert redact("short=abc") == "short=abc"


def test_multiple_long_key_values_redacted():
    assert (
        redact("a=1234567890123456 b=abcdefghijklmnop")
        == "a=[REDACTED] b=[REDACTED]"
    )


# ---- passthrough ----

def test_plain_text_unchanged():
    assert redact("hello world") == "hello world"


def test_empty_string_unchanged():
    assert redact("") == ""


def test_lowercase_bearer_redacted():
    assert redact("bearer abc123def456ghi789") == "Bearer [REDACTED]"


def test_non_ascii_value_unchanged():
    assert redact("token=секрет1234567890") == "token=секрет1234567890"


# ---- Basic Auth ----

def test_basic_auth_header_redacted():
    # "Authorization: Basic dXNlcjpwYXNzd29yZA==" (8+ chars after "Basic ")
    # → "Authorization: Basic [REDACTED]"
    assert redact("Authorization: Basic dXNlcjpwYXNzd29yZA==") == "Authorization: Basic [REDACTED]"


def test_basic_auth_case_insensitive():
    # "basic ABCDEFGH12345678" — regex is case-insensitive
    # substitution normalises prefix to "Basic"
    assert redact("basic ABCDEFGH12345678") == "Basic [REDACTED]"


def test_basic_auth_short_value_not_redacted():
    # "Basic abc1234" (7 chars — below threshold of 8) → unchanged
    assert redact("Basic abc1234") == "Basic abc1234"


def test_basic_auth_value_exact_8_chars_redacted():
    # "Basic ABCDEFGH" (exactly 8 chars) → "Basic [REDACTED]"
    assert redact("Basic ABCDEFGH") == "Basic [REDACTED]"


# ---- OAuth / secret key-value ----

def test_client_secret_redacted():
    # "client_secret=mysupersecret123" (value 4+ chars) → "client_secret=[REDACTED]"
    assert redact("client_secret=mysupersecret123") == "client_secret=[REDACTED]"


def test_access_token_redacted():
    # "access_token=ya29.a0AfH6SMB..." → "access_token=[REDACTED]"
    assert redact("access_token=ya29.a0AfH6SMB") == "access_token=[REDACTED]"


def test_refresh_token_redacted():
    # "refresh_token=1//0Gabcdef..." → "refresh_token=[REDACTED]"
    assert redact("refresh_token=1//0Gabcdef") == "refresh_token=[REDACTED]"


def test_api_secret_redacted():
    # "api_secret=abcd" (exactly 4 chars) → "api_secret=[REDACTED]"
    assert redact("api_secret=abcd") == "api_secret=[REDACTED]"


def test_secret_kv_short_value_not_redacted():
    # "client_secret=abc" (3 chars — below 4 threshold) → unchanged
    assert redact("client_secret=abc") == "client_secret=abc"


def test_secret_kv_in_url_encoded_form():
    # "client_id=myapp&client_secret=s3cr3tval"
    # client_id and client_secret are both in the pattern → both redacted
    result = redact("client_id=myapp&client_secret=s3cr3tval")
    assert "client_id=[REDACTED]" in result
    assert "client_secret=[REDACTED]" in result


def test_secret_kv_with_spaces_around_equals():
    # "access_token = mytoken1234" → "access_token = [REDACTED]"
    assert redact("access_token = mytoken1234") == "access_token = [REDACTED]"


def test_mixed_bearer_and_secret_kv_both_redacted():
    # "Authorization: Bearer abc123def456ghi789 client_secret=topsecret99"
    # → both are redacted independently
    result = redact("Authorization: Bearer abc123def456ghi789 client_secret=topsecret99")
    assert "Bearer [REDACTED]" in result
    assert "client_secret=[REDACTED]" in result
    assert "abc123def456ghi789" not in result
    assert "topsecret99" not in result
