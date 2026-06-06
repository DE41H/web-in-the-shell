import asyncio
import sqlite3
from pathlib import Path

import pytest

from network.session.manager import SessionCredentials
from persistence import SessionStore
from persistence.crypto import _CIPHERTEXT_PREFIX
from persistence.db import init_db


async def _seed(db_path: Path, host: str, creds: SessionCredentials) -> None:
    await init_db(db_path)
    async with SessionStore(db_path) as store:
        await store.save(host, creds)


async def test_session_store_round_trip(tmp_path: Path):
    db_path = tmp_path / "wits.db"
    await init_db(db_path)
    creds = SessionCredentials(
        cookies={"session": "abc123def456", "csrf": "tok-value-7890"},
        bearer_token="Bearer-tok-1234",
        csrf_token="csrf-abc-9999",
        extra_headers={"x-api-key": "k1", "x-client-id": "c1"},
    )
    async with SessionStore(db_path) as store:
        await store.save("api.example.com", creds)
        loaded = await store.get("api.example.com")
    assert loaded is not None
    assert loaded.cookies == creds.cookies
    assert loaded.bearer_token == creds.bearer_token
    assert loaded.csrf_token == creds.csrf_token
    assert loaded.extra_headers == creds.extra_headers


async def test_session_store_save_is_idempotent(tmp_path: Path):
    db_path = tmp_path / "wits.db"
    await init_db(db_path)
    creds = SessionCredentials(bearer_token="tok")
    async with SessionStore(db_path) as store:
        await store.save("h", creds)
        await store.save("h", creds)
        await store.save("h", creds)
        all_sessions = await store.list_all()
    assert len(all_sessions) == 1


async def test_session_store_save_overwrites_existing(tmp_path: Path):
    db_path = tmp_path / "wits.db"
    await init_db(db_path)
    async with SessionStore(db_path) as store:
        await store.save("h", SessionCredentials(bearer_token="old"))
        await store.save("h", SessionCredentials(bearer_token="new"))
        loaded = await store.get("h")
    assert loaded is not None
    assert loaded.bearer_token == "new"


async def test_session_store_get_missing_returns_none(tmp_path: Path):
    db_path = tmp_path / "wits.db"
    await init_db(db_path)
    async with SessionStore(db_path) as store:
        assert await store.get("nope.example.com") is None


async def test_session_store_host_isolation(tmp_path: Path):
    db_path = tmp_path / "wits.db"
    await init_db(db_path)
    async with SessionStore(db_path) as store:
        await store.save("a.example.com", SessionCredentials(bearer_token="a-tok"))
        await store.save("b.example.com", SessionCredentials(bearer_token="b-tok"))

        a = await store.get("a.example.com")
        b = await store.get("b.example.com")
    assert a is not None and a.bearer_token == "a-tok"
    assert b is not None and b.bearer_token == "b-tok"


async def test_session_store_empty_credentials_round_trip(tmp_path: Path):
    db_path = tmp_path / "wits.db"
    await init_db(db_path)
    creds = SessionCredentials()
    async with SessionStore(db_path) as store:
        await store.save("h", creds)
        loaded = await store.get("h")
    assert loaded is not None
    assert loaded.cookies == {}
    assert loaded.bearer_token is None
    assert loaded.csrf_token is None
    assert loaded.extra_headers == {}


async def test_session_store_delete_removes_only_target_host(tmp_path: Path):
    db_path = tmp_path / "wits.db"
    await init_db(db_path)
    async with SessionStore(db_path) as store:
        await store.save("a", SessionCredentials(bearer_token="a-tok"))
        await store.save("b", SessionCredentials(bearer_token="b-tok"))

        deleted = await store.delete("a")
        assert deleted == 1

        assert await store.get("a") is None
        assert await store.get("b") is not None


async def test_session_store_delete_missing_returns_zero(tmp_path: Path):
    db_path = tmp_path / "wits.db"
    await init_db(db_path)
    async with SessionStore(db_path) as store:
        assert await store.delete("never-existed") == 0


async def test_session_store_list_all_orders_by_updated_at_desc(tmp_path: Path):
    db_path = tmp_path / "wits.db"
    await init_db(db_path)
    async with SessionStore(db_path) as store:
        await store.save("older", SessionCredentials(bearer_token="o"))
        await asyncio_sleep(store)
        await store.save("newer", SessionCredentials(bearer_token="n"))

        all_sessions = await store.list_all()
    assert [h for h, _, _ in all_sessions] == ["newer", "older"]


async def test_session_store_list_all_empty(tmp_path: Path):
    db_path = tmp_path / "wits.db"
    await init_db(db_path)
    async with SessionStore(db_path) as store:
        assert await store.list_all() == []


async def test_session_store_clear_all(tmp_path: Path):
    db_path = tmp_path / "wits.db"
    await init_db(db_path)
    async with SessionStore(db_path) as store:
        await store.save("a", SessionCredentials(bearer_token="a"))
        await store.save("b", SessionCredentials(bearer_token="b"))

        deleted = await store.clear_all()
        assert deleted == 2
        assert await store.list_all() == []


async def test_session_store_clear_all_when_empty(tmp_path: Path):
    db_path = tmp_path / "wits.db"
    await init_db(db_path)
    async with SessionStore(db_path) as store:
        assert await store.clear_all() == 0


async def test_session_store_methods_require_async_context_manager(tmp_path: Path):
    db_path = tmp_path / "wits.db"
    await init_db(db_path)
    store = SessionStore(db_path)
    with pytest.raises(RuntimeError, match="async context manager"):
        await store.get("h")
    with pytest.raises(RuntimeError, match="async context manager"):
        await store.save("h", SessionCredentials())
    with pytest.raises(RuntimeError, match="async context manager"):
        await store.delete("h")
    with pytest.raises(RuntimeError, match="async context manager"):
        await store.list_all()
    with pytest.raises(RuntimeError, match="async context manager"):
        await store.clear_all()


async def test_session_store_does_not_redact_bearer_token(tmp_path: Path):
    db_path = tmp_path / "wits.db"
    await init_db(db_path)
    raw_token = "BearerLongTokenValueWithMoreThanSixteenChars"
    async with SessionStore(db_path) as store:
        await store.save("h", SessionCredentials(bearer_token=raw_token))

    conn = sqlite3.connect(db_path)
    try:
        row = conn.execute(
            "SELECT bearer_token FROM sessions WHERE host = ?", ("h",)
        ).fetchone()
    finally:
        conn.close()
    assert raw_token not in row[0]
    assert row[0].startswith(_CIPHERTEXT_PREFIX)
    assert "[REDACTED]" not in row[0]


async def test_session_store_creates_parent_dir(tmp_path: Path):
    db_path = tmp_path / "nested" / "deep" / "wits.db"
    await init_db(db_path)
    async with SessionStore(db_path) as store:
        await store.save("h", SessionCredentials(bearer_token="t"))


async def test_session_store_saves_encrypted_ciphertext_on_disk(tmp_path: Path):
    db_path = tmp_path / "wits.db"
    await init_db(db_path)
    creds = SessionCredentials(
        cookies={"session": "super-secret-cookie-value"},
        bearer_token="raw-bearer-token-1234",
        csrf_token="raw-csrf-token-5678",
        extra_headers={"x-api-key": "raw-api-key-9999"},
    )
    async with SessionStore(db_path) as store:
        await store.save("h", creds)

    conn = sqlite3.connect(db_path)
    try:
        row = conn.execute(
            "SELECT cookies, bearer_token, csrf_token, extra_headers "
            "FROM sessions WHERE host = ?",
            ("h",),
        ).fetchone()
    finally:
        conn.close()
    cookies_ct, bearer_ct, csrf_ct, extra_ct = row
    for col in (cookies_ct, bearer_ct, csrf_ct, extra_ct):
        assert col.startswith(_CIPHERTEXT_PREFIX)
    assert "super-secret-cookie-value" not in cookies_ct
    assert "raw-bearer-token-1234" not in bearer_ct
    assert "raw-csrf-token-5678" not in csrf_ct
    assert "raw-api-key-9999" not in extra_ct


async def test_session_store_encrypted_round_trip(tmp_path: Path):
    db_path = tmp_path / "wits.db"
    await init_db(db_path)
    creds = SessionCredentials(
        cookies={"session": "v1", "csrf": "v2"},
        bearer_token="btok",
        csrf_token="ctok",
        extra_headers={"x-api-key": "kv"},
    )
    async with SessionStore(db_path) as store:
        await store.save("h", creds)
        loaded = await store.get("h")
    assert loaded is not None
    assert loaded.cookies == creds.cookies
    assert loaded.bearer_token == creds.bearer_token
    assert loaded.csrf_token == creds.csrf_token
    assert loaded.extra_headers == creds.extra_headers


async def test_session_store_list_all_decrypts_ciphertext(tmp_path: Path):
    db_path = tmp_path / "wits.db"
    await init_db(db_path)
    creds_a = SessionCredentials(bearer_token="a-tok", cookies={"k": "av"})
    creds_b = SessionCredentials(bearer_token="b-tok", extra_headers={"x": "bv"})
    async with SessionStore(db_path) as store:
        await store.save("a.example.com", creds_a)
        await store.save("b.example.com", creds_b)
        all_sessions = await store.list_all()
    by_host = {h: c for h, c, _ in all_sessions}
    assert by_host["a.example.com"].bearer_token == "a-tok"
    assert by_host["a.example.com"].cookies == {"k": "av"}
    assert by_host["b.example.com"].bearer_token == "b-tok"
    assert by_host["b.example.com"].extra_headers == {"x": "bv"}


# ── New tests for H6 and M13 fixes ───────────────────────────────────────────

async def test_get_invalid_token_returns_none_with_warning(tmp_path: Path):
    """H6: decrypt failure in get() returns None and emits a warning."""
    import warnings as _warnings
    from unittest.mock import patch
    from cryptography.fernet import InvalidToken as _IT

    db_path = tmp_path / "wits.db"
    await init_db(db_path)
    creds = SessionCredentials(bearer_token="tok", csrf_token="csrf")
    async with SessionStore(db_path) as store:
        await store.save("h", creds)

    with patch("persistence.session_store.decrypt", side_effect=_IT("bad")):
        async with SessionStore(db_path) as store:
            with _warnings.catch_warnings(record=True) as w:
                _warnings.simplefilter("always")
                result = await store.get("h")

    assert result is None
    assert len(w) == 1
    assert "corrupt" in str(w[0].message).lower() or "rotation" in str(w[0].message).lower()


async def test_list_all_invalid_token_skips_row_with_warning(tmp_path: Path):
    """H6: decrypt failure in list_all() skips affected row and warns."""
    import warnings as _warnings
    from unittest.mock import patch
    from cryptography.fernet import InvalidToken as _IT

    db_path = tmp_path / "wits.db"
    await init_db(db_path)
    async with SessionStore(db_path) as store:
        await store.save("good.example.com", SessionCredentials(bearer_token="good-tok"))
        await store.save("bad.example.com", SessionCredentials(bearer_token="bad-tok"))

    call_count = {"n": 0}
    from persistence.crypto import decrypt as _real_decrypt

    def sometimes_fail(ct: str) -> str:
        call_count["n"] += 1
        if call_count["n"] == 3:
            raise _IT("simulated rotation")
        return _real_decrypt(ct)

    with patch("persistence.session_store.decrypt", side_effect=sometimes_fail):
        async with SessionStore(db_path) as store:
            with _warnings.catch_warnings(record=True) as w:
                _warnings.simplefilter("always")
                results = await store.list_all()

    assert len(results) == 1
    assert any(
        "corrupt" in str(wi.message).lower() or "rotation" in str(wi.message).lower()
        for wi in w
    )


async def test_saved_row_updated_at_is_utc_aware(tmp_path: Path):
    """M13: updated_at stored in sessions table is UTC-aware ISO timestamp."""
    from datetime import UTC

    db_path = tmp_path / "wits.db"
    await init_db(db_path)
    async with SessionStore(db_path) as store:
        await store.save("h", SessionCredentials(bearer_token="tok"))
        sessions = await store.list_all()

    assert len(sessions) == 1
    _, _, updated_at = sessions[0]
    # Must be timezone-aware and in UTC.
    assert updated_at.tzinfo is not None
    assert (
        updated_at.tzinfo == UTC
        or updated_at.utcoffset().total_seconds() == 0  # type: ignore[union-attr]
    )


# ---- helpers ----

async def asyncio_sleep(_store) -> None:
    await asyncio.sleep(0.01)
