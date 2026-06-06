"""End-to-end SessionStore test: subprocess saves, subprocess reads, keys match.

The store is per-host credential rehydration. Across process boundaries the
Fernet key (keyring primary, `~/.wits/fernet.key` 0600 fallback) must be
stable so that what one process writes, another can read.

This test does NOT exercise `main.py` — the mock pipeline never produces
session credentials. Instead it runs two minimal subprocess scripts that
import `SessionStore` and call `save`/`get`, and asserts the on-disk row
is encrypted ciphertext (not plaintext).
"""

import asyncio
import os
import sqlite3
import sys
from pathlib import Path

from persistence.crypto import _CIPHERTEXT_PREFIX
from persistence.db import init_db


_SAVE_RUNNER = """\
import asyncio, os, sys
from pathlib import Path
sys.path.insert(0, 'src')
from persistence.db import init_db
from persistence.session_store import SessionStore
from network.session.manager import SessionCredentials

async def _run():
    db = Path(os.environ['WITS_DB_PATH'])
    await init_db(db)
    async with SessionStore(db) as s:
        await s.save('example.com', SessionCredentials(
            cookies={'session': 'abc123'},
            bearer_token='bearer-secret-xyz',
            csrf_token='csrf-secret-123',
            extra_headers={'X-Custom': 'value-1'}))

asyncio.run(_run())
"""

_READ_RUNNER = """\
import asyncio, os, sys
from pathlib import Path
sys.path.insert(0, 'src')
from persistence.session_store import SessionStore

async def _run():
    db = Path(os.environ['WITS_DB_PATH'])
    async with SessionStore(db) as s:
        c = await s.get('example.com')
        print(repr(c.cookies))
        print(repr(c.bearer_token))
        print(repr(c.csrf_token))
        print(repr(c.extra_headers))

asyncio.run(_run())
"""

_INIT_RUNNER = """\
import asyncio, os, sys
from pathlib import Path
sys.path.insert(0, 'src')
from persistence.db import init_db

asyncio.run(init_db(Path(os.environ['WITS_DB_PATH'])))
"""


async def _run_subprocess(code: str, env: dict[str, str]) -> tuple[int, str, str]:
    proc = await asyncio.create_subprocess_exec(
        sys.executable, "-c", code,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
        env=env,
    )
    try:
        stdout, stderr = await asyncio.wait_for(proc.communicate(), timeout=30)
    finally:
        if proc.returncode is None:
            try:
                proc.kill()
                await proc.wait()
            except ProcessLookupError:
                pass
    return proc.returncode, stdout.decode(), stderr.decode()


async def test_session_store_round_trip_across_processes(tmp_path: Path, monkeypatch):
    """Write a session in one subprocess, read it in another; verify key stability."""
    db_path = tmp_path / "wits-session.db"
    monkeypatch.setenv("WITS_DB_PATH", str(db_path))

    env = os.environ.copy()
    env["WITS_DB_PATH"] = str(db_path)
    env["NO_COLOR"] = "1"
    env["PYTHONPATH"] = "src"

    rc, _, stderr = await _run_subprocess(_SAVE_RUNNER, env)
    assert rc == 0, f"save subprocess failed: {stderr}"
    assert db_path.exists()

    conn = sqlite3.connect(db_path)
    try:
        cur = conn.execute(
            "SELECT cookies, bearer_token, csrf_token, extra_headers "
            "FROM sessions WHERE host = ?", ("example.com",),
        )
        row = cur.fetchone()
    finally:
        conn.close()
    assert row is not None, "no row in sessions table"

    cookies_ct, bearer_ct, csrf_ct, extra_ct = row
    for label, val in (
        ("cookies", cookies_ct), ("bearer", bearer_ct),
        ("csrf", csrf_ct), ("extra", extra_ct),
    ):
        assert val.startswith(_CIPHERTEXT_PREFIX), (
            f"{label} should be Fernet-encrypted, got prefix={val[:10]!r}"
        )
    for label, ct_val, marker in (
        ("cookies", cookies_ct, "abc123"),
        ("bearer", bearer_ct, "bearer-secret-xyz"),
        ("csrf", csrf_ct, "csrf-secret-123"),
        ("extra", extra_ct, "value-1"),
    ):
        assert marker not in ct_val, (
            f"plaintext marker {marker!r} found in {label} ciphertext"
        )

    rc, stdout, stderr = await _run_subprocess(_READ_RUNNER, env)
    assert rc == 0, f"read subprocess failed: {stderr}"
    lines = [line for line in stdout.splitlines() if line.strip()]
    assert len(lines) == 4
    assert "'session': 'abc123'" in lines[0]
    assert "'bearer-secret-xyz'" in lines[1]
    assert "'csrf-secret-123'" in lines[2]
    assert "'X-Custom': 'value-1'" in lines[3]


async def test_session_store_keys_match_keyring_fallback(tmp_path: Path, monkeypatch):
    """Reading the DB with stdlib `sqlite3` (no Python keyring) decrypts correctly.

    The Fernet key must come from `~/.wits/fernet.key` 0600 fallback when
    keyring is unavailable, so the second process can decrypt what the
    first wrote.
    """
    db_path = tmp_path / "wits-keycheck.db"
    monkeypatch.setenv("WITS_DB_PATH", str(db_path))

    env = os.environ.copy()
    env["WITS_DB_PATH"] = str(db_path)
    env["NO_COLOR"] = "1"
    env["PYTHONPATH"] = "src"

    rc, _, stderr = await _run_subprocess(_SAVE_RUNNER, env)
    assert rc == 0, f"save subprocess failed: {stderr}"

    async def _decrypt_inline() -> tuple[str, str, str, str]:
        from persistence.session_store import SessionStore
        async with SessionStore(db_path) as s:
            creds = await s.get("example.com")
            assert creds is not None
            return (
                repr(creds.cookies),
                repr(creds.bearer_token),
                repr(creds.csrf_token),
                repr(creds.extra_headers),
            )

    cookies, bearer, csrf, extra = await _decrypt_inline()
    assert "'session': 'abc123'" in cookies
    assert "'bearer-secret-xyz'" in bearer
    assert "'csrf-secret-123'" in csrf
    assert "'X-Custom': 'value-1'" in extra


async def test_session_store_init_db_idempotent_across_processes(tmp_path: Path, monkeypatch):
    """`init_db` is safe to call from multiple processes — schema is IF NOT EXISTS."""
    db_path = tmp_path / "wits-init.db"
    monkeypatch.setenv("WITS_DB_PATH", str(db_path))

    env = os.environ.copy()
    env["WITS_DB_PATH"] = str(db_path)
    env["NO_COLOR"] = "1"
    env["PYTHONPATH"] = "src"

    for _ in range(3):
        rc, _, stderr = await _run_subprocess(_INIT_RUNNER, env)
        assert rc == 0, f"init_db subprocess failed: {stderr}"

    await init_db(db_path)
    async with __import__("persistence").ConvoStore(db_path) as convos:
        rows = await convos.list_all()
        assert rows == []
    async with __import__("persistence").SessionStore(db_path) as sessions:
        all_sessions = await sessions.list_all()
        assert all_sessions == []
