"""Secret string redaction — masks bearer tokens, JWTs, and long key-value secrets."""

import re

# Bearer <token> where token is 8+ chars of URL-safe base64 / JWT alphabet
BEARER_RE = re.compile(r"Bearer\s+([A-Za-z0-9._~+/=\-]{8,})", re.IGNORECASE)

# JWT-shaped strings: three base64url segments separated by dots
JWT_RE = re.compile(r"\bey[A-Za-z0-9_\-]+\.[A-Za-z0-9_\-]+\.[A-Za-z0-9_\-]+")

# key=<value> where value is 16+ alphanumeric chars (API keys, session tokens, etc.)
# Use (?<!\w) instead of \b so that keys preceded by " or : (JSON context) are
# also matched — \b requires a word character on the left, which silently skips
# patterns like `"token=abc123def456ghij"`.
KEY_VALUE_RE = re.compile(r"(?<!\w)(\w+)=([A-Za-z0-9]{16,})")

# Basic Auth: Basic <base64-encoded credentials>
BASIC_AUTH_RE = re.compile(r"Basic\s+([A-Za-z0-9+/=]{8,})", re.IGNORECASE)

# OAuth and common secret key-value pairs
SECRET_KV_RE = re.compile(
    r"(\b(?:client_secret|access_token|refresh_token|api_secret|client_id)\s*=\s*)([^\s&\"']{4,})",
    re.IGNORECASE,
)


def redact(text: str) -> str:
    """Mask bearer tokens, JWTs, and long key=value secrets in *text*."""
    text = SECRET_KV_RE.sub(r"\1[REDACTED]", text)
    text = BASIC_AUTH_RE.sub("Basic [REDACTED]", text)
    # JWT must be replaced before Bearer because a JWT can appear as a bearer token
    text = JWT_RE.sub("[JWT REDACTED]", text)
    text = BEARER_RE.sub("Bearer [REDACTED]", text)
    text = KEY_VALUE_RE.sub(r"\1=[REDACTED]", text)
    return text
