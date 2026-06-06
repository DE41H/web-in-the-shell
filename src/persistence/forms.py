"""Persistent store for auto-filled form field values."""

import re
import warnings
from pathlib import Path

import aiosqlite

from persistence.crypto import decrypt, encrypt
from persistence.db import DEFAULT_DB_PATH, utcnow_iso
from security.redact import redact as _redact

# Fields whose names match this pattern are never stored (too sensitive)
_SENSITIVE_NAME_RE = re.compile(
    r"password|passwd|pwd|cvv|ssn|pin|otp|secret|card.?num", re.IGNORECASE
)
_SENSITIVE_TYPES = {"password"}


def _is_sensitive(field_type: str, field_name: str) -> bool:
    return field_type in _SENSITIVE_TYPES or bool(_SENSITIVE_NAME_RE.search(field_name))


class FormFieldStore:
    """Async context manager for reading and writing saved form field values.

    Values are stored per (domain, field_name) pair. Sensitive fields
    (passwords, CVVs, SSNs, etc.) are rejected at write time.
    """

    def __init__(self, path: Path = DEFAULT_DB_PATH) -> None:
        self._path = path
        self._conn: aiosqlite.Connection | None = None

    async def __aenter__(self) -> "FormFieldStore":
        self._conn = await aiosqlite.connect(self._path)
        self._conn.row_factory = aiosqlite.Row
        return self

    async def __aexit__(self, *_: object) -> None:
        if self._conn:
            await self._conn.close()
            self._conn = None

    def _require_conn(self) -> aiosqlite.Connection:
        if self._conn is None:
            raise RuntimeError("Use 'async with FormFieldStore()' first.")
        return self._conn

    async def get(self, domain: str, field_name: str) -> str | None:
        """Return the saved value for (domain, field_name), or None if not found."""
        conn = self._require_conn()
        cur = await conn.execute(
            "SELECT value FROM form_fields WHERE domain = ? AND field_name = ?",
            (domain, field_name),
        )
        row = await cur.fetchone()
        if row is None:
            return None
        try:
            return decrypt(row["value"])
        except Exception as exc:
            warnings.warn(
                f"FormFieldStore.get: failed to decrypt value for ({domain!r}, "
                f"{field_name!r}): {exc}; returning None (key rotation or DB corruption).",
                stacklevel=2,
            )
            return None

    async def save(
        self, domain: str, field_name: str, field_type: str, value: str
    ) -> None:
        """Persist a form field value. Silently skips sensitive fields."""
        if _is_sensitive(field_type, field_name):
            return  # never store sensitive fields
        conn = self._require_conn()
        now = utcnow_iso()
        safe_value = encrypt(_redact(value))
        await conn.execute(
            """
            INSERT INTO form_fields (domain, field_name, field_type, value, updated_at)
            VALUES (?, ?, ?, ?, ?)
            ON CONFLICT(domain, field_name) DO UPDATE SET
                value = excluded.value,
                field_type = excluded.field_type,
                updated_at = excluded.updated_at
            """,
            (domain, field_name, field_type, safe_value, now),
        )
        await conn.commit()

    async def get_all_for_domain(self, domain: str) -> dict[str, str]:
        """Return all saved field values for the given domain as {field_name: value}."""
        conn = self._require_conn()
        cur = await conn.execute(
            "SELECT field_name, value FROM form_fields WHERE domain = ?",
            (domain,),
        )
        rows = await cur.fetchall()
        result: dict[str, str] = {}
        for row in rows:
            try:
                result[row["field_name"]] = decrypt(row["value"])
            except Exception as exc:
                warnings.warn(
                    f"FormFieldStore.get_all_for_domain: failed to decrypt value for "
                    f"({domain!r}, {row['field_name']!r}): {exc}; skipping row "
                    "(key rotation or DB corruption).",
                    stacklevel=2,
                )
        return result

    async def delete(self, domain: str, field_name: str) -> int:
        """Delete a single saved field. Returns 1 if deleted, 0 if not found."""
        conn = self._require_conn()
        cur = await conn.execute(
            "DELETE FROM form_fields WHERE domain = ? AND field_name = ?",
            (domain, field_name),
        )
        await conn.commit()
        return cur.rowcount

    async def clear_domain(self, domain: str) -> int:
        """Delete all saved fields for a domain. Returns number of rows deleted."""
        conn = self._require_conn()
        cur = await conn.execute(
            "DELETE FROM form_fields WHERE domain = ?", (domain,)
        )
        await conn.commit()
        return cur.rowcount
