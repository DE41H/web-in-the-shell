from pathlib import Path

import pytest

from network.session.manager import SessionCredentials, SessionManager
from persistence import SessionStore
from persistence.db import init_db


async def _store_with(db_path: Path, host: str, creds: SessionCredentials) -> SessionStore:
    await init_db(db_path)
    store = SessionStore(db_path)
    await store.__aenter__()
    await store.save(host, creds)
    return store


async def test_restore_fills_empty_fields(tmp_path: Path):
    db_path = tmp_path / "wits.db"
    stored = SessionCredentials(
        cookies={"session": "sess-tok-1234567890"},
        bearer_token="restored-tok",
        csrf_token="restored-csrf",
        extra_headers={"x-api-key": "k-from-store"},
    )
    store = await _store_with(db_path, "api.example.com", stored)
    try:
        sm = SessionManager()
        loaded = await sm.restore("api.example.com", store)
    finally:
        await store.__aexit__()

    assert loaded is True
    assert sm.credentials.cookies == stored.cookies
    assert sm.credentials.bearer_token == stored.bearer_token
    assert sm.credentials.csrf_token == stored.csrf_token
    assert sm.credentials.extra_headers == stored.extra_headers


async def test_restore_returns_false_when_host_missing(tmp_path: Path):
    db_path = tmp_path / "wits.db"
    await init_db(db_path)
    store = SessionStore(db_path)
    await store.__aenter__()
    try:
        sm = SessionManager()
        loaded = await sm.restore("never-saved.example.com", store)
    finally:
        await store.__aexit__()
    assert loaded is False
    assert sm.credentials.cookies == {}
    assert sm.credentials.bearer_token is None
    assert sm.credentials.csrf_token is None
    assert sm.credentials.extra_headers == {}


async def test_restore_does_not_overwrite_live_cookies(tmp_path: Path):
    db_path = tmp_path / "wits.db"
    stored = SessionCredentials(cookies={"session": "from-store-value-12345"})
    store = await _store_with(db_path, "h", stored)
    try:
        sm = SessionManager()
        sm.credentials.cookies = {"session": "from-live-value-67890"}
        await sm.restore("h", store)
    finally:
        await store.__aexit__()

    assert sm.credentials.cookies == {"session": "from-live-value-67890"}


async def test_restore_does_not_overwrite_live_bearer_token(tmp_path: Path):
    db_path = tmp_path / "wits.db"
    stored = SessionCredentials(bearer_token="store-tok")
    store = await _store_with(db_path, "h", stored)
    try:
        sm = SessionManager()
        sm.credentials.bearer_token = "live-tok"
        await sm.restore("h", store)
    finally:
        await store.__aexit__()
    assert sm.credentials.bearer_token == "live-tok"


async def test_restore_merges_extra_headers(tmp_path: Path):
    db_path = tmp_path / "wits.db"
    stored = SessionCredentials(extra_headers={"x-api-key": "from-store"})
    store = await _store_with(db_path, "h", stored)
    try:
        sm = SessionManager()
        sm.credentials.extra_headers = {"x-client-id": "live-cid"}
        await sm.restore("h", store)
    finally:
        await store.__aexit__()
    assert sm.credentials.extra_headers == {
        "x-api-key": "from-store",
        "x-client-id": "live-cid",
    }


async def test_persist_writes_current_credentials(tmp_path: Path):
    db_path = tmp_path / "wits.db"
    await init_db(db_path)
    sm = SessionManager()
    sm.credentials.cookies = {"session": "sess-tok-1234"}
    sm.credentials.bearer_token = "live-tok"
    sm.credentials.csrf_token = "live-csrf"
    sm.credentials.extra_headers = {"x-api-key": "live-k"}

    async with SessionStore(db_path) as store:
        await sm.persist("api.example.com", store)
        loaded = await store.get("api.example.com")

    assert loaded is not None
    assert loaded.cookies == sm.credentials.cookies
    assert loaded.bearer_token == sm.credentials.bearer_token
    assert loaded.csrf_token == sm.credentials.csrf_token
    assert loaded.extra_headers == sm.credentials.extra_headers


async def test_persist_then_restore_round_trip(tmp_path: Path):
    db_path = tmp_path / "wits.db"
    await init_db(db_path)
    sm1 = SessionManager()
    sm1.credentials.bearer_token = "round-trip-tok-1"
    sm1.credentials.csrf_token = "round-trip-csrf-1"
    sm1.credentials.cookies = {"k": "v"}
    sm1.credentials.extra_headers = {"x-h": "y"}

    async with SessionStore(db_path) as store:
        await sm1.persist("h", store)

    sm2 = SessionManager()
    async with SessionStore(db_path) as store:
        loaded = await sm2.restore("h", store)
    assert loaded is True
    assert sm2.credentials.bearer_token == "round-trip-tok-1"
    assert sm2.credentials.csrf_token == "round-trip-csrf-1"
    assert sm2.credentials.cookies == {"k": "v"}
    assert sm2.credentials.extra_headers == {"x-h": "y"}


async def test_restore_does_not_raise_when_store_unopened(tmp_path: Path):
    db_path = tmp_path / "wits.db"
    await init_db(db_path)
    sm = SessionManager()
    store = SessionStore(db_path)
    with pytest.raises(RuntimeError, match="async context manager"):
        await sm.restore("h", store)
