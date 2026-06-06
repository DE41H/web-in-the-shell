from __future__ import annotations

import asyncio
import os
from datetime import datetime
from typing import TYPE_CHECKING

from rich import box
from rich.console import Console
from rich.layout import Layout
from rich.live import Live
from rich.markup import escape
from rich.panel import Panel
from rich.table import Table
from rich.text import Text

from security.redact import redact as _redact

if TYPE_CHECKING:
    from ai.errors import ErrorInfo


# ---------------------------------------------------------------------------
# Environment detection
# ---------------------------------------------------------------------------

_NO_COLOR: bool = bool(os.environ.get("NO_COLOR") or os.environ.get("TERM") == "dumb")


# ---------------------------------------------------------------------------
# Cost table (input $/1K tokens, output $/1K tokens)
# ---------------------------------------------------------------------------

_COST_PER_1K: dict[str, tuple[float, float]] = {
    "claude-sonnet-4-6":            (0.003,    0.015),
    "claude-haiku-4-5-20251001":    (0.00025,  0.00125),
    "claude-opus-4-8":              (0.015,    0.075),
    "gpt-4o":                       (0.0025,   0.010),
    "gpt-4o-mini":                  (0.00015,  0.0006),
    "gpt-4-turbo":                  (0.010,    0.030),
    "llama-3.3-70b-versatile":      (0.00059,  0.00079),
    "llama-3.1-8b-instant":         (0.00005,  0.00008),
    "mixtral-8x7b-32768":           (0.00027,  0.00027),
    "gemini-2.0-flash":             (0.0001,   0.0004),
    "gemini-1.5-pro":               (0.00125,  0.005),
    "gemini-1.5-flash":             (0.000075, 0.0003),
    "meta-llama/Llama-3-70b-chat-hf": (0.0009, 0.0009),
    "meta-llama/Llama-3-8b-chat-hf":  (0.0002, 0.0002),
}


_STATUS_ICON: dict[str, str] = {
    "Idle":        "·",
    "Planning":    "◔",
    "Executing":   "▶",
    "Recovering":  "↻",
    "Complete":    "✓",
    "Failed":      "✗",
    "Interrupted": "!",
}


_STATUS_STYLE: dict[str, str] = {
    "Idle":        "dim white",
    "Planning":    "bold yellow",
    "Executing":   "bold cyan",
    "Recovering":  "bold red",
    "Complete":    "bold green",
    "Failed":      "bold red",
    "Interrupted": "dim yellow",
}


# ---------------------------------------------------------------------------
# AgentDisplay
# ---------------------------------------------------------------------------

class AgentDisplay:
    """
    Two-column Rich TUI.
    Left  — agent thought stream + status badge + optional step progress +
            pinned cost line + summary footer.
    Right — live network monitor (raw vs compacted bytes per intercepted
            endpoint).

    Plain-text fallback is activated automatically when NO_COLOR or TERM=dumb
    is detected in the environment.
    """

    def __init__(self) -> None:
        self._console = Console()
        self._thoughts: list[str] = []
        self._status: str = "Idle"
        self._intercepts: list[dict] = []
        self._step: tuple[int, int, str] | None = None
        self._total_tokens_in: int = 0
        self._total_tokens_out: int = 0
        self._cost_line: str = ""
        self._started_at: datetime = datetime.now()
        self._summary: str = ""
        self._errors: list[ErrorInfo] = []
        self._layout = Layout()
        self._layout.split_row(
            Layout(name="left",  ratio=1),
            Layout(name="right", ratio=1),
        )
        self._live: Live | None = None
        self._plain: bool = False

    # ------------------------------------------------------------------
    # Rendering helpers
    # ------------------------------------------------------------------

    def _format_elapsed(self) -> str:
        delta = datetime.now() - self._started_at
        seconds = int(delta.total_seconds())
        if seconds < 60:
            return f"{seconds}s"
        minutes, secs = divmod(seconds, 60)
        return f"{minutes}m{secs:02d}s"

    def _render_left(self) -> Panel:
        style = _STATUS_STYLE.get(self._status, "white")
        icon  = _STATUS_ICON.get(self._status, " ")
        body = Text()

        body.append(f"  {icon} ", style=style)
        body.append(f"{self._status}", style=style)
        body.append(f"  · {self._format_elapsed()}\n", style="dim white")

        if self._step is not None:
            step_num, total, label = self._step
            bar = self._render_progress_bar(step_num, total)
            body.append(
                f"  step {step_num}/{total}  {bar}  {label}\n",
                style=style,
            )

        body.append("\n")

        for line in self._thoughts[-20:]:
            body.append(f"  {escape(line)}\n", style="dim white")

        if self._errors:
            last = self._errors[-1]
            body.append("\n  " + "─" * 36 + "\n", style="dim white")
            err_label = "ERROR" if len(self._errors) == 1 else f"ERROR ({len(self._errors)})"
            body.append(f"  {err_label}\n", style="bold red")
            for line in last.to_lines():
                body.append(f"  {escape(line)}\n", style="red")

        if self._cost_line:
            body.append("\n  " + "─" * 36 + "\n", style="dim white")
            body.append(f"  {self._cost_line}\n", style="bold green")

        if self._summary:
            body.append("\n  " + "─" * 36 + "\n", style="dim white")
            body.append(f"  {escape(self._summary)}\n", style=style)

        return Panel(
            body,
            title="[bold cyan] WEB IN THE SHELL [/bold cyan]",
            border_style="cyan",
            box=box.DOUBLE_EDGE,
        )

    @staticmethod
    def _render_progress_bar(step: int, total: int, width: int = 12) -> str:
        if total <= 0:
            return " " * width
        filled = max(0, min(width, int(round(width * step / total))))
        return "█" * filled + "░" * (width - filled)

    def _render_right(self) -> Panel:
        table = Table(
            show_header=True,
            header_style="bold magenta",
            box=box.SIMPLE_HEAVY,
            expand=True,
            show_edge=False,
        )
        table.add_column("ENDPOINT", style="dim cyan", no_wrap=True, max_width=34)
        table.add_column("ST",      justify="center", max_width=4)
        table.add_column("RAW",     justify="right",  max_width=9)
        table.add_column("COMPACT", justify="right",  max_width=9)
        table.add_column("RATIO",   justify="right",  max_width=7)

        for cap in self._intercepts[-20:]:
            raw, compact, status = cap["raw_bytes"], cap["compact_bytes"], cap["status"]
            ratio = f"{int((1 - compact / max(raw, 1)) * 100)}%" if raw else "—"
            st_style = (
                "green" if 200 <= status < 300
                else "yellow" if status < 500
                else "red"
            )
            table.add_row(
                f"…{cap['url'][-33:]}",
                Text(str(status), style=st_style),
                f"{raw:,}b",
                f"{compact:,}b",
                Text(ratio, style="green"),
            )

        footer = Text()
        footer.append("\n  total intercepts: ", style="dim white")
        footer.append(f"{len(self._intercepts)}", style="bold magenta")
        if self._intercepts:
            total_raw = sum(c["raw_bytes"] for c in self._intercepts)
            total_compact = sum(c["compact_bytes"] for c in self._intercepts)
            if total_raw:
                saved_pct = int((1 - total_compact / total_raw) * 100)
                footer.append(
                    f"   ↓ saved {saved_pct}% "
                    f"({total_raw:,}b → {total_compact:,}b)",
                    style="green",
                )

        if self._errors:
            from ai.errors import ErrorCategory
            footer.append("\n  errors: ", style="dim white")
            counts: dict[ErrorCategory, int] = {}
            for e in self._errors:
                counts[e.category] = counts.get(e.category, 0) + 1
            for cat, count in counts.items():
                footer.append(f" {count}x {cat.value.upper()}", style="red")

        return Panel(
            table,
            title="[bold magenta] NETWORK MONITOR [/bold magenta]",
            border_style="magenta",
            box=box.DOUBLE_EDGE,
            subtitle=footer,
            subtitle_align="left",
        )

    def _refresh(self) -> None:
        if self._plain:
            return
        self._layout["left"].update(self._render_left())
        self._layout["right"].update(self._render_right())
        if self._live:
            self._live.refresh()

    # ------------------------------------------------------------------
    # Public interface — must remain stable for src/main.py
    # ------------------------------------------------------------------

    def set_status(self, status: str) -> None:
        self._status = status
        if self._plain:
            print(f"[STATUS] {status}")
            return
        self._refresh()

    def log_thought(self, thought: str) -> None:
        thought = _redact(thought)
        ts = datetime.now().strftime("%H:%M:%S")
        entry = f"[{ts}] {thought}"
        self._thoughts.append(entry)
        if self._plain:
            print(entry)
            return
        self._refresh()

    def log_intercept(
        self,
        url: str,
        status: int,
        raw_bytes: int,
        compact_bytes: int,
    ) -> None:
        url = _redact(url)
        self._intercepts.append(
            {
                "url": url,
                "status": status,
                "raw_bytes": raw_bytes,
                "compact_bytes": compact_bytes,
            }
        )
        if self._plain:
            ratio = (
                f"{int((1 - compact_bytes / max(raw_bytes, 1)) * 100)}%"
                if raw_bytes
                else "—"
            )
            print(
                f"[NETWORK] {url[-60:]}  status={status}"
                f"  raw={raw_bytes:,}b  compact={compact_bytes:,}b  ratio={ratio}"
            )
            return
        self._refresh()

    # ------------------------------------------------------------------
    # New methods
    # ------------------------------------------------------------------

    def log_cost(self, tokens_in: int, tokens_out: int, model: str) -> None:
        self._total_tokens_in  += tokens_in
        self._total_tokens_out += tokens_out

        rate_in, rate_out = _COST_PER_1K.get(model, (0.0, 0.0))
        total_cost = (
            self._total_tokens_in  / 1000.0 * rate_in
            + self._total_tokens_out / 1000.0 * rate_out
        )

        self._cost_line = (
            f"Cost: ${total_cost:.4f}"
            f"  (↑ {self._total_tokens_in:,} / ↓ {self._total_tokens_out:,} tokens)"
        )

        if self._plain:
            print(f"[COST] {self._cost_line}")
            return
        self._refresh()

    def log_step(self, step_num: int, total: int, label: str) -> None:
        self._step = (step_num, total, label)
        if self._plain:
            print(f"[STEP] {step_num}/{total}: {label}")
            return
        self._refresh()

    def set_summary(self, text: str) -> None:
        self._summary = _redact(text)
        if self._plain:
            print(f"[SUMMARY] {self._summary}")
            return
        self._refresh()

    def log_error(self, error: ErrorInfo) -> None:
        """Record a structured error from :mod:`ai.errors` for the operator.

        Behaviour:

        - The full error block (icon + title + detail + hint) is appended to
          the thought stream so it survives the 20-line window when more
          recent activity pushes it down.
        - The most recent error is also pinned under an ``ERROR`` banner at
          the bottom of the left panel, with a counter for repeat failures.
        - The status badge stays as the caller set it — this method is purely
          informational. The caller is expected to update status separately.

        Args:
            error: A structured :class:`ai.errors.ErrorInfo`.
        """
        self._errors.append(error)
        redacted = _redact("\n".join(error.to_lines()))
        self._thoughts.append(redacted)
        if self._plain:
            for line in error.to_lines():
                print(line)
            return
        self._refresh()

    def clear_error(self) -> None:
        """Clear the pinned error line and counter (thought stream keeps history)."""
        self._errors.clear()
        if self._plain:
            return
        self._refresh()

    async def countdown_exit(self, seconds: int) -> None:
        """Count down from *seconds* to 1, updating the status badge each tick,
        then set the status to ``"Complete"`` on a normal finish.

        In plain-text mode each tick prints a ``[DONE]`` line instead of
        updating the TUI.

        Catches ``KeyboardInterrupt`` and ``asyncio.CancelledError`` — both are
        suppressed and the method returns immediately without setting the final
        ``"Complete"`` status.

        Used by ``--no-interactive`` CI runs only; interactive REPL sessions use
        :meth:`wait_for_enter` instead.

        Args:
            seconds: Number of seconds to count down before finishing.
        """
        try:
            for n in range(seconds, 0, -1):
                if self._plain:
                    print(f"[DONE] exiting in {n}s...")
                else:
                    self._status = f"Done  ({n}s)"
                    self._refresh()
                await asyncio.sleep(1)
        except (KeyboardInterrupt, asyncio.CancelledError):
            return
        self._status = "Complete"
        if not self._plain:
            self._refresh()

    async def wait_for_enter(self, prompt: str = "Press Enter to close…") -> None:
        """Suspend the live display and block until the user presses Enter.

        Used by interactive REPL sessions so the operator can read the
        execution results before the alternate-screen teardown. Behaviour:

        - **Color mode:** the Rich ``Live`` alternate screen is stopped, the
          prompt is printed on the regular terminal, then ``input()`` blocks
          for a line. The ``Live`` is restarted and the display refreshed.
        - **Plain mode:** the prompt is printed to stdout and ``input()``
          blocks. No alternate screen is in use.
        - **EOF / SIGINT:** a missing stdin (piped input, EOF, or
          ``KeyboardInterrupt``) returns silently — never raises.

        Args:
            prompt: The line to show the operator. Default is
                ``"Press Enter to close…"``.
        """
        paused_live = False
        if not self._plain and self._live is not None:
            try:
                self._live.stop()
            except Exception:
                pass
            paused_live = True

        try:
            self._console.print(f"\n  [bold cyan]{prompt}[/bold cyan]")
            await asyncio.to_thread(input)
        except (EOFError, KeyboardInterrupt):
            pass
        finally:
            if paused_live and self._live is not None:
                try:
                    self._live.start()
                    self._refresh()
                except Exception:
                    pass

    # ------------------------------------------------------------------
    # Context manager
    # ------------------------------------------------------------------

    def __enter__(self) -> "AgentDisplay":
        if _NO_COLOR:
            self._plain = True
            return self

        self._live = Live(
            self._layout,
            console=self._console,
            screen=True,
            refresh_per_second=8,
        )
        self._live.__enter__()
        self._refresh()
        return self

    def __exit__(self, *args: object) -> None:
        if self._live:
            self._live.__exit__(*args)
