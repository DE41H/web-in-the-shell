import os
from pathlib import Path

import aiosqlite


DEFAULT_DB_PATH = Path(os.environ.get("WITS_DB_PATH", "./wits.db"))


_SCHEMA = [
    """
    CREATE TABLE IF NOT EXISTS convos (
        id          TEXT PRIMARY KEY,
        intent      TEXT NOT NULL,
        created_at  TEXT NOT NULL,
        updated_at  TEXT NOT NULL,
        messages    TEXT NOT NULL,
        result      TEXT
    )
    """,
    """
    CREATE INDEX IF NOT EXISTS convos_intent_updated
        ON convos(intent, updated_at DESC)
    """,
    """
    CREATE TABLE IF NOT EXISTS sessions (
        host          TEXT PRIMARY KEY,
        cookies       TEXT NOT NULL,
        bearer_token  TEXT,
        csrf_token    TEXT,
        extra_headers TEXT,
        updated_at    TEXT NOT NULL
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS form_fields (
        domain      TEXT NOT NULL,
        field_name  TEXT NOT NULL,
        field_type  TEXT NOT NULL DEFAULT 'text',
        value       TEXT NOT NULL,
        updated_at  TEXT NOT NULL,
        PRIMARY KEY (domain, field_name)
    )
    """,
]


async def init_db(path: Path = DEFAULT_DB_PATH) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    async with aiosqlite.connect(path) as conn:
        await conn.execute("PRAGMA journal_mode=WAL")
        await conn.execute("PRAGMA foreign_keys=ON")
        for stmt in _SCHEMA:
            await conn.execute(stmt)
        await conn.commit()


async def journal_mode(path: Path = DEFAULT_DB_PATH) -> str:
    async with aiosqlite.connect(path) as conn:
        cur = await conn.execute("PRAGMA journal_mode")
        row = await cur.fetchone()
        return row[0]
