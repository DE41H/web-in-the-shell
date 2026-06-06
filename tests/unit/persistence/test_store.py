import asyncio
import sqlite3
from datetime import datetime, UTC
from pathlib import Path

import pytest

from persistence.crypto import _CIPHERTEXT_PREFIX, decrypt
from persistence.db import init_db
from persistence.models import Convo, ConvoMessage
from persistence.store import ConvoStore


def _convo(
    convo_id: str = "abc-123",
    intent: str = "intent-A",
    messages: list[ConvoMessage] | None = None,
    result: dict | None = None,
    created_at: datetime | None = None,
    updated_at: datetime | None = None,
) -> Convo:
    now = datetime(2026, 6, 6, 12, 0, 0, tzinfo=UTC)
    return Convo(
        id=convo_id,
        intent=intent,
        created_at=created_at or now,
        updated_at=updated_at or now,
        messages=messages or [ConvoMessage(role="user", content="hello")],
        result=result,
    )


async def test_round_trip(tmp_path: Path):
    db_path = tmp_path / "wits.db"
    await init_db(db_path)
    async with ConvoStore(db_path) as store:
        original = _convo(
            messages=[
                ConvoMessage(role="user", content="hi"),
                ConvoMessage(
                    role="assistant",
                    content="hello there",
                    tool_calls=[{"id": "1", "type": "function", "function": {"name": "x"}}],
                ),
                ConvoMessage(role="tool", tool_call_id="1", content="ok", name="x"),
            ],
            result={"ok": True, "value": 42},
        )
        await store.save(original)
        loaded = await store.get_latest_for_intent("intent-A")
        assert loaded is not None
        assert loaded.id == original.id
        assert loaded.intent == original.intent
        assert loaded.created_at == original.created_at
        assert loaded.updated_at == original.updated_at
        assert loaded.result == {"ok": True, "value": 42}
        assert [m.model_dump() for m in loaded.messages] == [
            m.model_dump() for m in original.messages
        ]


def test_to_llm_messages_drops_none_fields():
    convo = _convo(
        messages=[
            ConvoMessage(role="user", content="hi"),
            ConvoMessage(role="assistant", content="ok"),
        ],
    )
    out = convo.to_llm_messages()
    assert out == [
        {"role": "user", "content": "hi"},
        {"role": "assistant", "content": "ok"},
    ]
    for msg in out:
        assert "tool_calls" not in msg
        assert "tool_call_id" not in msg
        assert "name" not in msg


async def test_intent_isolation(tmp_path: Path):
    db_path = tmp_path / "wits.db"
    await init_db(db_path)
    async with ConvoStore(db_path) as store:
        await store.save(_convo(intent="intent-A"))
        await store.save(_convo(convo_id="xyz-999", intent="intent-B"))

        a = await store.get_latest_for_intent("intent-A")
        b = await store.get_latest_for_intent("intent-B")
        none = await store.get_latest_for_intent("intent-C")

        assert a is not None and a.id == "abc-123"
        assert b is not None and b.id == "xyz-999"
        assert none is None


async def test_redact_on_write_strips_bearer(tmp_path: Path):
    db_path = tmp_path / "wits.db"
    await init_db(db_path)
    secret = "Bearer abc123def456ghi789"
    convo = _convo(
        messages=[
            ConvoMessage(role="user", content=f"please call {secret}"),
        ],
        result={"token": secret, "ok": True},
    )
    async with ConvoStore(db_path) as store:
        await store.save(convo)

    conn = sqlite3.connect(db_path)
    try:
        cur = conn.execute("SELECT messages, result FROM convos WHERE id = ?", (convo.id,))
        messages_ct, result_ct = cur.fetchone()
    finally:
        conn.close()

    assert messages_ct.startswith(_CIPHERTEXT_PREFIX)
    assert result_ct.startswith(_CIPHERTEXT_PREFIX)
    assert "abc123def456ghi789" not in messages_ct
    assert "abc123def456ghi789" not in result_ct

    messages_json = decrypt(messages_ct)
    result_json = decrypt(result_ct)
    assert "[REDACTED]" in messages_json
    assert "[REDACTED]" in result_json


async def test_clear_removes_only_target_intent(tmp_path: Path):
    db_path = tmp_path / "wits.db"
    await init_db(db_path)
    async with ConvoStore(db_path) as store:
        await store.save(_convo(intent="intent-A"))
        await store.save(_convo(convo_id="id-2", intent="intent-A"))
        await store.save(_convo(convo_id="id-3", intent="intent-B"))

        deleted = await store.clear("intent-A")
        assert deleted == 2

        assert await store.get_latest_for_intent("intent-A") is None
        assert await store.get_latest_for_intent("intent-B") is not None


async def test_get_latest_returns_most_recent(tmp_path: Path):
    db_path = tmp_path / "wits.db"
    await init_db(db_path)
    older = datetime(2026, 1, 1, 12, 0, 0, tzinfo=UTC)
    newer = datetime(2026, 6, 6, 12, 0, 0, tzinfo=UTC)

    async with ConvoStore(db_path) as store:
        await store.save(_convo(convo_id="old", updated_at=older))
        await store.save(_convo(convo_id="new", updated_at=newer))

        latest = await store.get_latest_for_intent("intent-A")
        assert latest is not None
        assert latest.id == "new"


async def test_concurrent_writes_are_serialized(tmp_path: Path):
    db_path = tmp_path / "wits.db"
    await init_db(db_path)
    async with ConvoStore(db_path) as store:
        await asyncio.gather(
            *[store.save(_convo(convo_id=f"c-{i}", intent="intent-X")) for i in range(10)]
        )

        latest = await store.get_latest_for_intent("intent-X")
        assert latest is not None
        assert latest.id.startswith("c-")


async def test_non_string_content_is_left_alone(tmp_path: Path):
    db_path = tmp_path / "wits.db"
    await init_db(db_path)
    list_content = [{"type": "text", "text": "hello"}]
    convo = _convo(
        messages=[ConvoMessage(role="user", content=list_content)],
    )
    async with ConvoStore(db_path) as store:
        await store.save(convo)
        loaded = await store.get_latest_for_intent("intent-A")
    assert loaded is not None
    assert loaded.messages[0].content == list_content


async def test_methods_require_async_context_manager(tmp_path: Path):
    db_path = tmp_path / "wits.db"
    await init_db(db_path)
    store = ConvoStore(db_path)
    with pytest.raises(RuntimeError, match="async context manager"):
        await store.get_latest_for_intent("anything")
    with pytest.raises(RuntimeError, match="async context manager"):
        await store.save(_convo())
    with pytest.raises(RuntimeError, match="async context manager"):
        await store.clear("anything")
    with pytest.raises(RuntimeError, match="async context manager"):
        await store.list_all()
    with pytest.raises(RuntimeError, match="async context manager"):
        await store.clear_all()


async def test_list_all_returns_all_intents(tmp_path: Path):
    db_path = tmp_path / "wits.db"
    await init_db(db_path)
    older = datetime(2026, 1, 1, 12, 0, 0, tzinfo=UTC)
    newer = datetime(2026, 6, 6, 12, 0, 0, tzinfo=UTC)
    async with ConvoStore(db_path) as store:
        await store.save(_convo(convo_id="a", intent="intent-A", updated_at=older))
        await store.save(_convo(convo_id="b", intent="intent-B", updated_at=newer))

        all_convos = await store.list_all()
        assert [c.id for c in all_convos] == ["b", "a"]


async def test_clear_all_removes_every_intent(tmp_path: Path):
    db_path = tmp_path / "wits.db"
    await init_db(db_path)
    async with ConvoStore(db_path) as store:
        await store.save(_convo(intent="intent-A"))
        await store.save(_convo(convo_id="id-2", intent="intent-B"))
        await store.save(_convo(convo_id="id-3", intent="intent-C"))

        deleted = await store.clear_all()
        assert deleted == 3
        assert await store.list_all() == []


async def test_save_stores_encrypted_ciphertext_on_disk(tmp_path: Path):
    db_path = tmp_path / "wits.db"
    await init_db(db_path)
    convo = _convo(
        messages=[ConvoMessage(role="user", content="hello world")],
        result={"ok": True, "value": 42},
    )
    async with ConvoStore(db_path) as store:
        await store.save(convo)

    import aiosqlite
    async with aiosqlite.connect(db_path) as conn:
        conn.row_factory = aiosqlite.Row
        cur = await conn.execute(
            "SELECT messages, result FROM convos WHERE id = ?", (convo.id,)
        )
        row = await cur.fetchone()
    messages_ct, result_ct = row["messages"], row["result"]
    assert messages_ct.startswith(_CIPHERTEXT_PREFIX)
    assert result_ct.startswith(_CIPHERTEXT_PREFIX)
    plaintext_messages = decrypt(messages_ct)
    plaintext_result = decrypt(result_ct)
    assert "hello world" in plaintext_messages
    assert '"ok": true' in plaintext_result
    assert messages_ct != plaintext_messages
    assert result_ct != plaintext_result


async def test_save_then_get_recovers_plaintext(tmp_path: Path):
    db_path = tmp_path / "wits.db"
    await init_db(db_path)
    original = _convo(
        messages=[
            ConvoMessage(role="user", content="hello world"),
            ConvoMessage(role="assistant", content="hi back"),
        ],
        result={"answer": 42},
    )
    async with ConvoStore(db_path) as store:
        await store.save(original)
        loaded = await store.get_latest_for_intent("intent-A")
    assert loaded is not None
    assert loaded.messages[0].content == "hello world"
    assert loaded.messages[1].content == "hi back"
    assert loaded.result == {"answer": 42}


async def test_list_all_decrypts_ciphertext(tmp_path: Path):
    db_path = tmp_path / "wits.db"
    await init_db(db_path)
    async with ConvoStore(db_path) as store:
        await store.save(_convo(convo_id="a", intent="intent-A"))
        await store.save(_convo(convo_id="b", intent="intent-B"))
        convos = await store.list_all()
    by_id = {c.id: c for c in convos}
    assert set(by_id) == {"a", "b"}
    assert by_id["a"].messages[0].content == "hello"
    assert by_id["b"].messages[0].content == "hello"


async def test_encrypted_row_is_backwards_compatible_with_plaintext(tmp_path: Path):
    db_path = tmp_path / "wits.db"
    await init_db(db_path)
    async with ConvoStore(db_path) as store:
        await store.save(_convo(messages=[ConvoMessage(role="user", content="hi")]))

    conn = sqlite3.connect(db_path)
    try:
        row = conn.execute(
            "SELECT messages, result FROM convos WHERE id = ?", ("abc-123",)
        ).fetchone()
    finally:
        conn.close()
    messages_ct, _ = row
    assert messages_ct.startswith(_CIPHERTEXT_PREFIX)
    payload = decrypt(messages_ct)
    loaded = __import__("json").loads(payload)
    assert loaded[0]["content"] == "hi"


# ── New tests for C4 and H6 fixes ─────────────────────────────────────────────

async def test_list_content_text_blocks_are_redacted(tmp_path: Path):
    """C4: list-typed message content with 'text' dicts gets redacted."""
    db_path = tmp_path / "wits.db"
    await init_db(db_path)
    secret = "Bearer abc123def456ghi789"
    list_content = [
        {"type": "text", "text": f"result: {secret}"},
        {"type": "image_url", "image_url": {"url": "https://example.com/img.png"}},
    ]
    convo = _convo(messages=[ConvoMessage(role="user", content=list_content)])
    async with ConvoStore(db_path) as store:
        await store.save(convo)
        loaded = await store.get_latest_for_intent("intent-A")

    assert loaded is not None
    content = loaded.messages[0].content
    assert isinstance(content, list)
    # The text block's "text" value must have been redacted.
    assert "abc123def456ghi789" not in content[0]["text"]
    assert "[REDACTED]" in content[0]["text"]
    # Non-text blocks pass through unchanged.
    assert content[1] == {"type": "image_url", "image_url": {"url": "https://example.com/img.png"}}


async def test_list_content_str_items_are_redacted(tmp_path: Path):
    """C4: plain strings inside a list content are also redacted."""
    db_path = tmp_path / "wits.db"
    await init_db(db_path)
    secret = "Bearer abc123def456ghi789"
    list_content = [f"token is {secret}", "plain safe string"]
    convo = _convo(messages=[ConvoMessage(role="user", content=list_content)])
    async with ConvoStore(db_path) as store:
        await store.save(convo)
        loaded = await store.get_latest_for_intent("intent-A")

    assert loaded is not None
    content = loaded.messages[0].content
    assert isinstance(content, list)
    assert "abc123def456ghi789" not in content[0]
    assert "[REDACTED]" in content[0]
    assert content[1] == "plain safe string"


async def test_list_content_no_text_key_passes_through(tmp_path: Path):
    """C4: dict items without a 'text' key are passed through unchanged."""
    db_path = tmp_path / "wits.db"
    await init_db(db_path)
    list_content = [{"type": "tool_use", "id": "call_abc", "name": "my_tool"}]
    convo = _convo(messages=[ConvoMessage(role="assistant", content=list_content)])
    async with ConvoStore(db_path) as store:
        await store.save(convo)
        loaded = await store.get_latest_for_intent("intent-A")

    assert loaded is not None
    assert loaded.messages[0].content == list_content


async def test_get_latest_invalid_token_returns_none_with_warning(tmp_path: Path):
    """H6: InvalidToken on get_latest_for_intent returns None and warns."""
    import warnings as _warnings
    from unittest.mock import patch

    db_path = tmp_path / "wits.db"
    await init_db(db_path)
    convo = _convo()
    async with ConvoStore(db_path) as store:
        await store.save(convo)

    # Patch decrypt to simulate a key-rotation InvalidToken error.
    from cryptography.fernet import InvalidToken as _IT

    with patch("persistence.store.decrypt", side_effect=_IT("bad token")):
        async with ConvoStore(db_path) as store:
            with _warnings.catch_warnings(record=True) as w:
                _warnings.simplefilter("always")
                result = await store.get_latest_for_intent("intent-A")

    assert result is None
    assert len(w) == 1
    assert "corrupt" in str(w[0].message).lower() or "rotation" in str(w[0].message).lower()


async def test_list_all_skips_undecryptable_rows_with_warning(tmp_path: Path):
    """H6: InvalidToken in list_all skips the affected row and warns."""
    import warnings as _warnings
    from unittest.mock import patch

    db_path = tmp_path / "wits.db"
    await init_db(db_path)
    # Save two rows; capture the exact ciphertext of the first to identify it.
    async with ConvoStore(db_path) as store:
        await store.save(_convo(convo_id="ok-row", intent="intent-A"))
        await store.save(_convo(convo_id="bad-row", intent="intent-B"))

    # Read the raw ciphertext for "bad-row" so we can fail exactly that call.
    import aiosqlite
    async with aiosqlite.connect(db_path) as conn:
        cur = await conn.execute(
            "SELECT messages FROM convos WHERE id = ?", ("bad-row",)
        )
        bad_ct = (await cur.fetchone())[0]

    from cryptography.fernet import InvalidToken as _IT
    real_decrypt = decrypt

    def targeted_fail(ct: str) -> str:
        if ct == bad_ct:
            raise _IT("simulated key rotation")
        return real_decrypt(ct)

    with patch("persistence.store.decrypt", side_effect=targeted_fail):
        async with ConvoStore(db_path) as store:
            with _warnings.catch_warnings(record=True) as w:
                _warnings.simplefilter("always")
                results = await store.list_all()

    # One row is skipped; the other is returned.
    assert len(results) == 1
    assert results[0].id == "ok-row"
    assert any(
        "corrupt" in str(wi.message).lower() or "rotation" in str(wi.message).lower()
        for wi in w
    )
