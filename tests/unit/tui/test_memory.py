from datetime import datetime, UTC
from pathlib import Path
from unittest.mock import MagicMock

import pytest
from rich.console import Console

from persistence import Convo, ConvoMessage
from persistence import ConvoStore
from persistence.db import init_db
from tui.memory import manage_memory


def _convo(
    convo_id: str = "abc-123",
    intent: str = "intent-A",
    messages: list[ConvoMessage] | None = None,
    result: dict | None = None,
    updated_at: datetime | None = None,
) -> Convo:
    now = datetime(2026, 6, 6, 12, 0, 0, tzinfo=UTC)
    return Convo(
        id=convo_id,
        intent=intent,
        created_at=now,
        updated_at=updated_at or now,
        messages=messages or [ConvoMessage(role="user", content="hi")],
        result=result,
    )


class _InputQueue:
    def __init__(self, *answers: str) -> None:
        self._answers = list(answers)

    def __call__(self, _prompt: str) -> str:
        if not self._answers:
            raise EOFError("no more scripted inputs")
        return self._answers.pop(0)


def _never_confirm(_prompt: str, default: bool = False) -> bool:
    raise AssertionError("confirm_provider should not be called for this command")


def _console() -> Console:
    return Console(record=True, width=120, file=MagicMock())


async def test_manage_memory_lists_all_with_data(tmp_path: Path):
    db_path = tmp_path / "wits.db"
    await init_db(db_path)
    async with ConvoStore(db_path) as store:
        await store.save(_convo(intent="Create a user", convo_id="a-1"))
        await store.save(_convo(intent="Delete the database", convo_id="b-2"))

    console = _console()
    queue = _InputQueue("list", "quit")
    await manage_memory(
        db_path, console, input_provider=queue, confirm_provider=_never_confirm
    )
    out = console.export_text()
    assert "Stored conversations" in out
    assert "Create a user" in out
    assert "Delete the database" in out


async def test_manage_memory_lists_empty(tmp_path: Path):
    db_path = tmp_path / "wits.db"
    console = _console()
    queue = _InputQueue("list", "quit")
    await manage_memory(
        db_path, console, input_provider=queue, confirm_provider=_never_confirm
    )
    out = console.export_text()
    assert "(no conversations stored)" in out


async def test_manage_memory_quit_returns_immediately(tmp_path: Path):
    db_path = tmp_path / "wits.db"
    console = _console()
    queue = _InputQueue("quit")
    await manage_memory(
        db_path, console, input_provider=queue, confirm_provider=_never_confirm
    )


async def test_manage_memory_q_alias_works(tmp_path: Path):
    db_path = tmp_path / "wits.db"
    console = _console()
    queue = _InputQueue("q")
    await manage_memory(
        db_path, console, input_provider=queue, confirm_provider=_never_confirm
    )


async def test_manage_memory_help_prints_commands(tmp_path: Path):
    db_path = tmp_path / "wits.db"
    console = _console()
    queue = _InputQueue("help", "quit")
    await manage_memory(
        db_path, console, input_provider=queue, confirm_provider=_never_confirm
    )
    out = console.export_text()
    assert "list" in out
    assert "view <intent>" in out
    assert "clear <intent>" in out
    assert "clear-all" in out
    assert "quit" in out


async def test_manage_memory_view_existing_intent(tmp_path: Path):
    db_path = tmp_path / "wits.db"
    await init_db(db_path)
    async with ConvoStore(db_path) as store:
        await store.save(_convo(
            convo_id="view-1",
            intent="Create a user",
            messages=[
                ConvoMessage(role="user", content="Create a user"),
                ConvoMessage(role="assistant", content="I will create it."),
            ],
        ))

    console = _console()
    queue = _InputQueue("view Create a user", "quit")
    await manage_memory(
        db_path, console, input_provider=queue, confirm_provider=_never_confirm
    )
    out = console.export_text()
    assert "I will create it." in out
    assert "Create a user" in out


async def test_manage_memory_view_unknown_intent_reports_error(tmp_path: Path):
    db_path = tmp_path / "wits.db"
    console = _console()
    queue = _InputQueue("view NoSuchIntent", "quit")
    await manage_memory(
        db_path, console, input_provider=queue, confirm_provider=_never_confirm
    )
    out = console.export_text()
    assert "no conversation found" in out
    assert "NoSuchIntent" in out


async def test_manage_memory_view_missing_argument_reports_usage(tmp_path: Path):
    db_path = tmp_path / "wits.db"
    console = _console()
    queue = _InputQueue("view", "quit")
    await manage_memory(
        db_path, console, input_provider=queue, confirm_provider=_never_confirm
    )
    out = console.export_text()
    assert "usage" in out


async def test_manage_memory_clear_confirmed_removes_intent(tmp_path: Path):
    db_path = tmp_path / "wits.db"
    await init_db(db_path)
    async with ConvoStore(db_path) as store:
        await store.save(_convo(intent="intent-A"))
        await store.save(_convo(convo_id="id-2", intent="intent-A"))
        await store.save(_convo(convo_id="id-3", intent="intent-B"))

    console = _console()
    queue = _InputQueue("clear intent-A", "quit")

    def _confirm(_prompt: str, default: bool = False) -> bool:
        return True

    await manage_memory(
        db_path, console, input_provider=queue, confirm_provider=_confirm
    )
    out = console.export_text()
    assert "deleted 2" in out

    async with ConvoStore(db_path) as store:
        assert await store.get_latest_for_intent("intent-A") is None
        assert await store.get_latest_for_intent("intent-B") is not None


async def test_manage_memory_clear_aborted_keeps_data(tmp_path: Path):
    db_path = tmp_path / "wits.db"
    await init_db(db_path)
    async with ConvoStore(db_path) as store:
        await store.save(_convo(intent="intent-A"))

    console = _console()
    queue = _InputQueue("clear intent-A", "quit")

    def _confirm(_prompt: str, default: bool = False) -> bool:
        return False

    await manage_memory(
        db_path, console, input_provider=queue, confirm_provider=_confirm
    )
    out = console.export_text()
    assert "aborted" in out

    async with ConvoStore(db_path) as store:
        assert await store.get_latest_for_intent("intent-A") is not None


async def test_manage_memory_clear_all_removes_everything(tmp_path: Path):
    db_path = tmp_path / "wits.db"
    await init_db(db_path)
    async with ConvoStore(db_path) as store:
        await store.save(_convo(intent="intent-A"))
        await store.save(_convo(convo_id="id-2", intent="intent-B"))

    console = _console()
    queue = _InputQueue("clear-all", "quit")

    def _confirm(_prompt: str, default: bool = False) -> bool:
        return True

    await manage_memory(
        db_path, console, input_provider=queue, confirm_provider=_confirm
    )
    out = console.export_text()
    assert "deleted 2" in out

    async with ConvoStore(db_path) as store:
        assert await store.list_all() == []


async def test_manage_memory_clear_all_aborted_keeps_data(tmp_path: Path):
    db_path = tmp_path / "wits.db"
    await init_db(db_path)
    async with ConvoStore(db_path) as store:
        await store.save(_convo(intent="intent-A"))

    console = _console()
    queue = _InputQueue("clear-all", "quit")

    def _confirm(_prompt: str, default: bool = False) -> bool:
        return False

    await manage_memory(
        db_path, console, input_provider=queue, confirm_provider=_confirm
    )
    out = console.export_text()
    assert "aborted" in out

    async with ConvoStore(db_path) as store:
        assert await store.get_latest_for_intent("intent-A") is not None


async def test_manage_memory_clear_missing_argument_reports_usage(tmp_path: Path):
    db_path = tmp_path / "wits.db"
    console = _console()
    queue = _InputQueue("clear", "quit")
    await manage_memory(
        db_path, console, input_provider=queue, confirm_provider=_never_confirm
    )
    out = console.export_text()
    assert "usage" in out


async def test_manage_memory_unknown_command_warns(tmp_path: Path):
    db_path = tmp_path / "wits.db"
    console = _console()
    queue = _InputQueue("bogus", "quit")
    await manage_memory(
        db_path, console, input_provider=queue, confirm_provider=_never_confirm
    )
    out = console.export_text()
    assert "unknown command" in out
    assert "bogus" in out


async def test_manage_memory_eof_returns_cleanly(tmp_path: Path):
    db_path = tmp_path / "wits.db"
    console = _console()
    queue = _InputQueue()
    await manage_memory(
        db_path, console, input_provider=queue, confirm_provider=_never_confirm
    )


async def test_manage_memory_keyboard_interrupt_returns_cleanly(tmp_path: Path):
    db_path = tmp_path / "wits.db"
    console = _console()

    def _interrupt(_prompt: str) -> str:
        raise KeyboardInterrupt

    await manage_memory(
        db_path, console, input_provider=_interrupt, confirm_provider=_never_confirm
    )


async def test_manage_memory_creates_parent_dir(tmp_path: Path):
    db_path = tmp_path / "nested" / "deep" / "wits.db"
    console = _console()
    queue = _InputQueue("quit")
    await manage_memory(
        db_path, console, input_provider=queue, confirm_provider=_never_confirm
    )
    assert db_path.exists()


async def test_manage_memory_empty_input_reprompts(tmp_path: Path):
    db_path = tmp_path / "wits.db"
    console = _console()
    queue = _InputQueue("", "  ", "quit")
    await manage_memory(
        db_path, console, input_provider=queue, confirm_provider=_never_confirm
    )


async def test_manage_memory_view_renders_list_content_as_string(tmp_path: Path):
    """A message whose content is a list gets stringified in the panel."""
    db_path = tmp_path / "wits.db"
    await init_db(db_path)
    async with ConvoStore(db_path) as store:
        await store.save(_convo(
            convo_id="list-content",
            intent="Create a post",
            messages=[
                ConvoMessage(role="user", content="Create a post"),
                ConvoMessage(role="assistant", content=["step 1", "step 2"]),
            ],
        ))

    console = _console()
    queue = _InputQueue("view Create a post", "quit")
    await manage_memory(
        db_path, console, input_provider=queue, confirm_provider=_never_confirm
    )
    out = console.export_text()
    assert "['step 1', 'step 2']" in out


async def test_manage_memory_view_renders_none_content_as_empty(tmp_path: Path):
    """A message whose content is None renders as the empty string."""
    db_path = tmp_path / "wits.db"
    await init_db(db_path)
    async with ConvoStore(db_path) as store:
        await store.save(_convo(
            convo_id="none-content",
            intent="Get posts",
            messages=[
                ConvoMessage(role="user", content="Get posts"),
                ConvoMessage(role="assistant", content=None, tool_calls=[{"name": "fetch"}]),
            ],
        ))

    console = _console()
    queue = _InputQueue("view Get posts", "quit")
    await manage_memory(
        db_path, console, input_provider=queue, confirm_provider=_never_confirm
    )
    out = console.export_text()
    assert "assistant" in out
    assert "Get posts" in out


async def test_manage_memory_propagates_cancellation(tmp_path: Path):
    """asyncio.CancelledError is re-raised (not swallowed) so the event loop can react."""
    import asyncio

    db_path = tmp_path / "wits.db"
    console = _console()

    class _CancelImmediately:
        def __call__(self, _prompt: str) -> str:
            raise asyncio.CancelledError

    with pytest.raises(asyncio.CancelledError):
        await manage_memory(
            db_path, console,
            input_provider=_CancelImmediately(),
            confirm_provider=_never_confirm,
        )
