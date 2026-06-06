from __future__ import annotations

import asyncio
from collections.abc import Callable
from pathlib import Path

from rich.console import Console
from rich.panel import Panel
from rich.prompt import Confirm, Prompt
from rich.table import Table

from persistence import Convo, ConvoStore, init_db


_PROMPT: Callable[[str], str] = Prompt.ask


def _CONFIRM(prompt: str, default: bool = False) -> bool:  # noqa: N802
    """Wrap Confirm.ask so ``default`` is always passed as a keyword argument."""
    return Confirm.ask(prompt, default=default)


def _help_text() -> str:
    return (
        "[bold cyan]Memory commands[/bold cyan]\n"
        "  [green]list[/green]              — show all stored conversations\n"
        "  [green]view <intent>[/green]     — show the most recent conversation for that intent\n"
        "  [green]clear <intent>[/green]    — delete all conversations for that intent\n"
        "  [green]clear-all[/green]         — delete every stored conversation\n"
        "  [green]help[/green]              — show this message\n"
        "  [green]quit[/green] | [green]q[/green]          — leave the memory panel"
    )


def _render_list(conv_convos: list[Convo]) -> Table:
    table = Table(
        title="Stored conversations",
        show_header=True,
        header_style="bold magenta",
        expand=True,
    )
    table.add_column("Intent",  style="cyan", overflow="fold")
    table.add_column("ID",      style="dim")
    table.add_column("Updated", style="green")
    table.add_column("Msgs",    justify="right")
    table.add_column("Result",  style="dim", overflow="fold")
    for convo in conv_convos:
        result = convo.result or {}
        endpoint = str(result.get("endpoint") or "—")
        status = result.get("status_code", "—")
        result_str = f"{endpoint}  {status}"
        table.add_row(
            convo.intent,
            convo.id,
            convo.updated_at.isoformat(timespec="seconds"),
            str(len(convo.messages)),
            result_str,
        )
    return table


def _render_view(convo: Convo) -> Panel:
    msg_table = Table(
        show_header=True,
        header_style="bold yellow",
        expand=True,
        title=(
            f"ID: {convo.id}    Intent: {convo.intent}    "
            f"Created: {convo.created_at.isoformat(timespec='seconds')}    "
            f"Updated: {convo.updated_at.isoformat(timespec='seconds')}    "
            f"Messages: {len(convo.messages)}    "
            f"Result: {convo.result if convo.result else '—'}"
        ),
    )
    msg_table.add_column("#",       justify="right", style="dim", no_wrap=True)
    msg_table.add_column("Role",    style="cyan", no_wrap=True)
    msg_table.add_column("Content", overflow="fold")

    for i, msg in enumerate(convo.messages, 1):
        content = msg.content
        if isinstance(content, list):
            content = str(content)
        elif content is None:
            content = ""
        msg_table.add_row(str(i), msg.role, str(content))

    return Panel(
        msg_table,
        title=f"[bold cyan]Conversation {convo.id[:8]}[/bold cyan]",
        border_style="cyan",
    )


async def _dispatch_command(
    cmd: str,
    store: ConvoStore,
    console: Console,
    *,
    confirm_provider: Callable[[str, bool], bool],
) -> bool:
    parts = cmd.strip().split()
    if not parts:
        return True

    head, tail = parts[0].lower(), parts[1:]

    if head in ("quit", "q", "exit"):
        return False

    if head == "help":
        console.print(_help_text())
        return True

    if head == "list":
        all_convos = await store.list_all()
        if not all_convos:
            console.print("[dim](no conversations stored)[/dim]")
            return True
        console.print(_render_list(all_convos))
        return True

    if head == "view":
        if not tail:
            console.print("[red]usage:[/red] view <intent>")
            return True
        intent = " ".join(tail)
        convo = await store.get_latest_for_intent(intent)
        if convo is None:
            console.print(f"[red]no conversation found for intent:[/red] {intent!r}")
            return True
        console.print(_render_view(convo))
        return True

    if head == "clear":
        if not tail:
            console.print("[red]usage:[/red] clear <intent>")
            return True
        intent = " ".join(tail)
        if not confirm_provider(
            f"Delete all conversations for intent {intent!r}?", False
        ):
            console.print("[dim]aborted[/dim]")
            return True
        deleted = await store.clear(intent)
        console.print(f"[green]deleted {deleted} conversation(s)[/green]")
        return True

    if head == "clear-all":
        if not confirm_provider("Delete ALL stored conversations?", False):
            console.print("[dim]aborted[/dim]")
            return True
        deleted = await store.clear_all()
        console.print(f"[green]deleted {deleted} conversation(s)[/green]")
        return True

    console.print(f"[red]unknown command:[/red] {head!r}  (type 'help')")
    return True


async def manage_memory(
    db_path: Path,
    console: Console,
    *,
    input_provider: Callable[[str], str] | None = None,
    confirm_provider: Callable[[str, bool], bool] | None = None,
) -> None:
    """Interactive Rich-based memory management panel.

    Reads commands from stdin (via ``rich.prompt.Prompt.ask``) and writes a
    table/panel to ``console``. Returns when the user types ``quit`` (or
    raises EOF / KeyboardInterrupt).

    The ``input_provider`` and ``confirm_provider`` parameters are escape
    hatches for tests; in production they default to ``Prompt.ask`` and
    ``Confirm.ask`` respectively.
    """
    ask = input_provider or _PROMPT
    confirm = confirm_provider or _CONFIRM

    db_path.parent.mkdir(parents=True, exist_ok=True)
    await init_db(db_path)

    console.print(_help_text())
    console.print()

    try:
        async with ConvoStore(db_path) as store:
            while True:
                try:
                    cmd = ask("[bold magenta]memory>[/bold magenta] ")
                except (EOFError, KeyboardInterrupt):
                    console.print()
                    return
                if not await _dispatch_command(
                    cmd, store, console, confirm_provider=confirm
                ):
                    return
    except asyncio.CancelledError:
        raise
