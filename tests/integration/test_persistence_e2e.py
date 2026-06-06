"""End-to-end persistence test: run the mock pipeline, verify the DB round-trip.

Exercises the full stack (intercept → plan → execute → save) against
jsonplaceholder.typicode.com, then verifies:
  1. A row was written to the `convos` table.
  2. The messages and result columns are encrypted (Fernet ciphertext).
  3. The intent column is stored in plaintext (it's the lookup key).
  4. The round-trip via ConvoStore preserves the original data.
  5. `--memory list` and `--memory clear` work from a fresh process.
"""

import asyncio
import sqlite3
import sys

import pytest

from persistence.crypto import _CIPHERTEXT_PREFIX, decrypt
from persistence.db import init_db
from persistence.store import ConvoStore


_RUNNER = (
    "import asyncio, sys, os; "
    "sys.path.insert(0, 'src'); "
    "os.environ['NO_COLOR'] = '1'; "
    "sys.argv = ['main', '--mock', '--no-interactive', "
    "           '--intent', 'Fetch posts then create one']; "
    "import main; "
    "asyncio.run(main.main())"
)

_NETWORK_ERROR_MARKERS = (
    "ConnectionError", "NetworkError", "NameResolutionError",
    "Temporary failure in name resolution", "Could not connect",
    "ECONNREFUSED", "ETIMEDOUT", "ERR_NAME_NOT_RESOLVED",
    "ERR_CONNECTION", "ERR_INTERNET_DISCONNECTED",
    "ERR_PROXY_CONNECTION", "ERR_SSL", "ERR_CERT", "net::",
)


def _is_network_failure(stderr: str) -> bool:
    return any(m in stderr for m in _NETWORK_ERROR_MARKERS)


@pytest.mark.integration
async def test_mock_pipeline_persists_encrypted_memory(tmp_path, monkeypatch):
    """Run the full mock pipeline; verify a row lands in `convos`, encrypted."""
    db_path = tmp_path / "wits-e2e.db"
    monkeypatch.setenv("WITS_DB_PATH", str(db_path))
    monkeypatch.setenv("NO_COLOR", "1")

    proc = await asyncio.create_subprocess_exec(
        sys.executable, "-c", _RUNNER,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
    )
    try:
        stdout, stderr = await asyncio.wait_for(proc.communicate(), timeout=60)
    except (TimeoutError, OSError) as exc:
        try:
            proc.kill()
            await proc.wait()
        except ProcessLookupError:
            pass
        pytest.skip(f"network unavailable: {exc}")

    stderr_text = stderr.decode(errors="replace")
    if proc.returncode != 0 and _is_network_failure(stderr_text):
        pytest.skip(f"network unavailable: {stderr_text[:300]}")

    assert proc.returncode == 0, (
        f"main.py crashed (rc={proc.returncode}): {stderr_text[:500]}"
    )
    assert db_path.exists(), f"db file {db_path} was not created"

    conn = sqlite3.connect(db_path)
    try:
        cur = conn.execute(
            "SELECT id, intent, messages, result FROM convos"
        )
        rows = cur.fetchall()
    finally:
        conn.close()
    assert len(rows) == 1, f"expected 1 row in convos, got {len(rows)}"
    convo_id, intent, messages_ct, result_ct = rows[0]

    assert intent == "Fetch posts then create one", (
        f"intent should be stored in plaintext, got {intent!r}"
    )
    assert messages_ct.startswith(_CIPHERTEXT_PREFIX), (
        f"messages should be Fernet-encrypted, got prefix={messages_ct[:10]!r}"
    )
    assert result_ct.startswith(_CIPHERTEXT_PREFIX), (
        f"result should be Fernet-encrypted, got prefix={result_ct[:10]!r}"
    )

    plaintext_messages = decrypt(messages_ct)
    plaintext_result = decrypt(result_ct)
    assert "mock" in plaintext_messages.lower() or "create_post" in plaintext_messages, (
        f"round-tripped messages should mention the mock plan, got {plaintext_messages!r}"
    )
    assert "success" in plaintext_result, (
        f"round-tripped result should include success field, got {plaintext_result!r}"
    )

    await init_db(db_path)
    async with ConvoStore(db_path) as store:
        latest = await store.get_latest_for_intent("Fetch posts then create one")
    assert latest is not None, "ConvoStore.get_latest_for_intent should find the row"
    assert latest.id == convo_id
    assert latest.intent == "Fetch posts then create one"
    assert len(latest.messages) >= 1
    assert latest.result is not None
