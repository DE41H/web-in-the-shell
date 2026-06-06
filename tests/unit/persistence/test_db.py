import asyncio
import sqlite3
from pathlib import Path

from persistence.db import init_db, journal_mode


async def test_init_creates_file(tmp_path: Path):
    db_path = tmp_path / "wits.db"
    assert not db_path.exists()
    await init_db(db_path)
    assert db_path.exists()


async def test_init_sets_wal_mode(tmp_path: Path):
    db_path = tmp_path / "wits.db"
    await init_db(db_path)
    mode = await journal_mode(db_path)
    assert mode.lower() == "wal"


def _read_schema(db_path: Path) -> tuple[tuple | None, tuple | None, list[tuple]]:
    conn = sqlite3.connect(db_path)
    try:
        cur = conn.execute(
            "SELECT name FROM sqlite_master WHERE type='table' AND name='convos'"
        )
        table_row = cur.fetchone()
        cur = conn.execute(
            "SELECT name FROM sqlite_master WHERE type='index' AND name='convos_intent_updated'"
        )
        index_row = cur.fetchone()
        cur = conn.execute("PRAGMA index_info(convos_intent_updated)")
        index_cols = cur.fetchall()
    finally:
        conn.close()
    return table_row, index_row, index_cols


async def test_init_creates_table_and_index(tmp_path: Path):
    db_path = tmp_path / "wits.db"
    await init_db(db_path)
    table_row, index_row, index_cols = await asyncio.to_thread(_read_schema, db_path)
    assert table_row == ("convos",)
    assert index_row == ("convos_intent_updated",)
    assert [r[2] for r in index_cols] == ["intent", "updated_at"]


async def test_init_creates_parent_dir(tmp_path: Path):
    db_path = tmp_path / "nested" / "deeper" / "wits.db"
    await init_db(db_path)
    assert db_path.exists()


async def test_init_is_idempotent(tmp_path: Path):
    db_path = tmp_path / "wits.db"
    await init_db(db_path)
    await init_db(db_path)
    table_row, index_row, _ = await asyncio.to_thread(_read_schema, db_path)
    assert table_row == ("convos",)
    assert index_row == ("convos_intent_updated",)
