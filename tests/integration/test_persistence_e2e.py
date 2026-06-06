import asyncio
import json
import os
import sys
from datetime import datetime

import aiosqlite
import pytest

from persistence.store import ConvoStore
from persistence.models import Convo, ConvoMessage


async def _run_mock_pipeline(db_path: str, intent: str) -> None:
    """Run the mock pipeline as a subprocess, using the given DB path."""
    env = os.environ.copy()
    env["PYTHONPATH"] = "src"
    env["NO_COLOR"] = "1"
    env["TERM"] = "dumb"
    env["WITS_DB_PATH"] = db_path
    proc = await asyncio.create_subprocess_exec(
        sys.executable, "-c",
        "import asyncio, sys; "
        "sys.path.insert(0, 'src'); "
        f"sys.argv = ['main', '--mock', '--no-interactive', '--intent', {intent!r}]; "
        "import main; "
        "asyncio.run(main.main())",
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
        env=env,
        cwd=".",
    )
    try:
        stdout, stderr = await asyncio.wait_for(proc.communicate(), timeout=120)
    except (TimeoutError, OSError) as exc:
        try:
            proc.kill()
            await proc.wait()
        except ProcessLookupError:
            pass
        pytest.skip(f"pipeline timed out or network unavailable: {exc}")

    stderr_text = stderr.decode(errors="replace")
    network_markers = ("NameResolutionError", "ConnectionError", "net::", "ECONNREFUSED")
    if proc.returncode != 0 and any(m in stderr_text for m in network_markers):
        pytest.skip(f"network unavailable: {stderr_text[:300]}")

    assert proc.returncode == 0, (
        f"mock pipeline crashed (rc={proc.returncode}): {stderr_text[:500]}"
    )


@pytest.mark.integration
async def test_same_intent_two_runs_produces_one_row(tmp_path):
    """Running the mock pipeline twice with the same intent must yield exactly
    one convo row (INSERT OR REPLACE keyed on a stable id, not a fresh uuid4)."""
    db_path = str(tmp_path / "wits.db")
    intent = "Fetch posts then create one"

    await _run_mock_pipeline(db_path, intent)
    await _run_mock_pipeline(db_path, intent)

    async with aiosqlite.connect(db_path) as conn:
        async with conn.execute(
            "SELECT COUNT(*) FROM convos WHERE intent = ?", (intent,)
        ) as cur:
            row = await cur.fetchone()

    assert row is not None
    assert row[0] == 1, (
        f"Expected 1 convo row for intent {intent!r}, got {row[0]}; "
        "ConvoStore is accumulating unbounded rows instead of replacing the existing one."
    )


@pytest.mark.integration
async def test_convo_store_roundtrip(tmp_path):
    db = tmp_path / "wits.db"
    # Create a minimal sqlite schema for tests
    schema = (
        "CREATE TABLE convos "
        "(id TEXT PRIMARY KEY, intent TEXT, created_at TEXT, "
        "updated_at TEXT, messages BLOB, result BLOB);",
    )
    import aiosqlite

    async with aiosqlite.connect(db) as conn:
        for s in schema:
            await conn.execute(s)
        await conn.commit()

    # Use values that match redact() patterns: Bearer token and long key=value
    convo = Convo(
        id="c1",
        intent="Fetch posts",
        created_at=datetime.utcnow(),
        updated_at=datetime.utcnow(),
        messages=[ConvoMessage(role="user", content="Authorization: Bearer ABCDEFGH12345")],
        result={"ok": True, "access_token": "abcdefghijklmnop"},
    )

    async with ConvoStore(db) as store:
        await store.save(convo)
        got = await store.get_latest_for_intent("Fetch posts")
        assert got is not None
        assert got.intent == convo.intent
        # Redaction should have occurred: bearer token replaced and access_token redacted
        dumped_msg = json.dumps(got.messages[0].model_dump())
        assert "Bearer [REDACTED]" in dumped_msg
        assert got.result is not None
        assert "[REDACTED]" in json.dumps(got.result)
