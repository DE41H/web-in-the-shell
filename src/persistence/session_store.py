from __future__ import annotations

import json
from datetime import datetime
from pathlib import Path

import aiosqlite

from network.session.manager import SessionCredentials

from persistence.crypto import decrypt, encrypt


class SessionStore:
    """Async context manager for persistent session-credential storage.

    One row per host; ``INSERT OR REPLACE`` makes ``save`` idempotent. The store
    is the only place outside the live browser that holds bearer tokens, CSRF
    tokens, and cookie values.

    Threat model
    ------------
    Sessions are stored **without redaction** — the entire purpose of the
    store is to round-trip credentials across runs, so redacting the row
    would break reload. In place of redaction, the sensitive fields
    (``bearer_token``, ``csrf_token``, ``cookies``, ``extra_headers``) are
    encrypted at rest with a keyring-stored Fernet key (see
    ``persistence.crypto``). The DB file is local-only (excluded from
    version control via ``.gitignore``). Do not move session storage to a
    network or shared filesystem without re-evaluating this.
    """

    def __init__(self, path: Path) -> None:
        self._path = path
        self._conn: aiosqlite.Connection | None = None

    async def __aenter__(self) -> SessionStore:
        self._conn = await aiosqlite.connect(self._path)
        return self

    async def __aexit__(self, *_: object) -> None:
        if self._conn is not None:
            await self._conn.close()
            self._conn = None

    def _require_conn(self) -> aiosqlite.Connection:
        if self._conn is None:
            raise RuntimeError(
                "SessionStore must be used as an async context manager: "
                "`async with SessionStore(path) as store:`."
            )
        return self._conn

    async def get(self, host: str) -> SessionCredentials | None:
        conn = self._require_conn()
        async with conn.execute(
            """
            SELECT cookies, bearer_token, csrf_token, extra_headers
            FROM sessions
            WHERE host = ?
            """,
            (host,),
        ) as cur:
            row = await cur.fetchone()
        if row is None:
            return None
        cookies_plain = decrypt(row[0]) if row[0] else None
        bearer_plain = decrypt(row[1]) if row[1] else None
        csrf_plain = decrypt(row[2]) if row[2] else None
        extra_plain = decrypt(row[3]) if row[3] else None
        return SessionCredentials(
            cookies=json.loads(cookies_plain) if cookies_plain else {},
            bearer_token=bearer_plain,
            csrf_token=csrf_plain,
            extra_headers=json.loads(extra_plain) if extra_plain else {},
        )

    async def save(self, host: str, creds: SessionCredentials) -> None:
        conn = self._require_conn()
        now = datetime.now().isoformat()
        await conn.execute(
            """
            INSERT OR REPLACE INTO sessions
                (host, cookies, bearer_token, csrf_token, extra_headers, updated_at)
            VALUES (?, ?, ?, ?, ?, ?)
            """,
            (
                host,
                encrypt(json.dumps(creds.cookies)),
                encrypt(creds.bearer_token) if creds.bearer_token else None,
                encrypt(creds.csrf_token) if creds.csrf_token else None,
                encrypt(json.dumps(creds.extra_headers)),
                now,
            ),
        )
        await conn.commit()

    async def delete(self, host: str) -> int:
        conn = self._require_conn()
        cur = await conn.execute("DELETE FROM sessions WHERE host = ?", (host,))
        await conn.commit()
        rowcount = cur.rowcount
        await cur.close()
        return rowcount

    async def list_all(self) -> list[tuple[str, SessionCredentials, datetime]]:
        conn = self._require_conn()
        async with conn.execute(
            "SELECT host, cookies, bearer_token, csrf_token, extra_headers, "
            "updated_at FROM sessions ORDER BY updated_at DESC"
        ) as cur:
            rows = await cur.fetchall()
        out: list[tuple[str, SessionCredentials, datetime]] = []
        for host, cookies_ct, bearer_ct, csrf_ct, extra_ct, updated_at in rows:
            cookies_plain = decrypt(cookies_ct) if cookies_ct else None
            bearer_plain = decrypt(bearer_ct) if bearer_ct else None
            csrf_plain = decrypt(csrf_ct) if csrf_ct else None
            extra_plain = decrypt(extra_ct) if extra_ct else None
            creds = SessionCredentials(
                cookies=json.loads(cookies_plain) if cookies_plain else {},
                bearer_token=bearer_plain,
                csrf_token=csrf_plain,
                extra_headers=json.loads(extra_plain) if extra_plain else {},
            )
            out.append((host, creds, datetime.fromisoformat(updated_at)))
        return out

    async def clear_all(self) -> int:
        conn = self._require_conn()
        cur = await conn.execute("DELETE FROM sessions")
        await conn.commit()
        rowcount = cur.rowcount
        await cur.close()
        return rowcount
