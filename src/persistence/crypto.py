"""Field-level encryption for the on-disk sqlite stores.

Sensitive fields in ``convos`` (``messages``, ``result``) and ``sessions``
(``bearer_token``, ``csrf_token``, ``cookies``, ``extra_headers``) are
stored as Fernet ciphertext. A short sentinel prefix (``enc:v1:``) marks
each encrypted value so reads can dispatch on it and stay
backwards-compatible with an unencrypted ``wits.db`` left over from v0.1.

The 32-byte Fernet key is loaded from the OS keyring (service
``web-in-the-shell``, user ``wits-fernet-key-v1``). If the keyring
backend is missing or fails, a fallback key file is written to
``~/.wits/fernet.key`` with ``0600`` permissions. Both backends must fail
before a ``RuntimeError`` is raised — the call site should never silently
revert to plaintext.
"""

from __future__ import annotations

import re
from pathlib import Path

from cryptography.fernet import Fernet


_SERVICE_NAME = "web-in-the-shell"
_KEY_USERNAME = "wits-fernet-key-v1"
_CIPHERTEXT_PREFIX = "enc:v1:"

_KEY_B64_PATTERN = re.compile(r"[A-Za-z0-9_\-]{43}=")


def _is_valid_b64_key(s: str | None) -> bool:
    if not s:
        return False
    return _KEY_B64_PATTERN.fullmatch(s) is not None


def _home() -> Path:
    return Path.home()


def _keyring_get(username: str) -> str | None:
    import keyring

    return keyring.get_password(_SERVICE_NAME, username)


def _keyring_set(username: str, value: str) -> None:
    import keyring

    keyring.set_password(_SERVICE_NAME, username, value)


def _load_or_create_file_key() -> bytes:
    fallback = _home() / ".wits" / "fernet.key"
    if fallback.exists():
        content = fallback.read_text(encoding="utf-8").strip()
        if _is_valid_b64_key(content):
            return content.encode("ascii")

    b64_key = Fernet.generate_key()
    try:
        fallback.parent.mkdir(parents=True, exist_ok=True)
        fallback.write_text(b64_key.decode("ascii"), encoding="utf-8")
        try:
            fallback.chmod(0o600)
        except (OSError, NotImplementedError, PermissionError):
            pass
        return b64_key
    except Exception as e:
        raise RuntimeError("no keyring backend and no fallback key file") from e


def _load_or_create_key() -> bytes:
    try:
        stored = _keyring_get(_KEY_USERNAME)
        if _is_valid_b64_key(stored):
            return stored.encode("ascii")
        b64_key = Fernet.generate_key()
        _keyring_set(_KEY_USERNAME, b64_key.decode("ascii"))
        return b64_key
    except Exception:
        return _load_or_create_file_key()


def encrypt(plaintext: str) -> str:
    """Encrypt *plaintext* as Fernet ciphertext with the ``enc:v1:`` prefix."""
    token = Fernet(_load_or_create_key()).encrypt(plaintext.encode("utf-8"))
    return _CIPHERTEXT_PREFIX + token.decode("ascii")


def decrypt(ciphertext: str) -> str:
    """Decrypt a Fernet-encrypted string produced by :func:`encrypt`.

    Values that do not start with the ``enc:v1:`` sentinel are returned
    unchanged so the store stays backwards-compatible with rows written
    before encryption was added.
    """
    if not ciphertext.startswith(_CIPHERTEXT_PREFIX):
        return ciphertext
    payload = ciphertext[len(_CIPHERTEXT_PREFIX) :]
    return Fernet(_load_or_create_key()).decrypt(payload.encode("ascii")).decode("utf-8")
