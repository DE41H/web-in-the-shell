import asyncio
import os
from datetime import datetime

from rich import box
from rich.console import Console
from rich.layout import Layout
from rich.live import Live
from rich.markup import escape
from rich.panel import Panel

from security.redact import redact as _redact
from rich.table import Table
from rich.text import Text


# ---------------------------------------------------------------------------
# Environment detection
# ---------------------------------------------------------------------------

_NO_COLOR: bool = bool(os.environ.get("NO_COLOR") or os.environ.get("TERM") == "dumb")


# ---------------------------------------------------------------------------
# Cost table (input $/1K tokens, output $/1K tokens)
# ---------------------------------------------------------------------------

_COST_PER_1K: dict[str, tuple[float, float]] = {
    # Anthropic
    "claude-sonnet-4-6":            (0.003,    0.015),
    "claude-haiku-4-5-20251001":    (0.00025,  0.00125),
    "claude-opus-4-8":              (0.015,    0.075),
    # OpenAI
    "gpt-4o":                       (0.0025,   0.010),
    "gpt-4o-mini":                  (0.00015,  0.0006),
    "gpt-4-turbo":                  (0.010,    0.030),
    # Groq (prices negligible but track tokens)
    "llama-3.3-70b-versatile":      (0.00059,  0.00079),
    "llama-3.1-8b-instant":         (0.00005,  0.00008),
    "mixtral-8x7b-32768":           (0.00027,  0.00027),
    # Gemini
    "gemini-2.0-flash":             (0.0001,   0.0004),
    "gemini-1.5-pro":               (0.00125,  0.005),
    "gemini-1.5-flash":             (0.000075, 0.0003),
    # Together AI
    "meta-llama/Llama-3-70b-chat-hf": (0.0009, 0.0009),
    "meta-llama/Llama-3-8b-chat-hf":  (0.0002, 0.0002),
}


# ---------------------------------------------------------------------------
# Status colour map
# ---------------------------------------------------------------------------

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
            pinned cost line.
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

        # Step progress  (step_num, total, label)  or None
        self._step: tuple[int, int, str] | None = None

        # Token / cost tracking
        self._total_tokens_in: int = 0
        self._total_tokens_out: int = 0
        self._cost_line: str = ""

        # Rich layout
        self._layout = Layout()
        self._layout.split_row(
            Layout(name="left",  ratio=1),
            Layout(name="right", ratio=1),
        )
        self._live: Live | None = None

        # Plain-text mode flag (set in __enter__ if _NO_COLOR)
        self._plain: bool = False

    # ------------------------------------------------------------------
    # Rendering helpers
    # ------------------------------------------------------------------

    def _render_left(self) -> Panel:
        style = _STATUS_STYLE.get(self._status, "white")
        body = Text()

        # Status badge
        body.append(f"  [{self._status}]\n", style=style)

        # Step progress (just below the status badge)
        if self._step is not None:
            step_num, total, label = self._step
            body.append(
                f"  step {step_num}/{total}: {label}\n",
                style=style,
            )

        body.append("\n")

        # Thought stream (most recent 20 lines)
        for line in self._thoughts[-20:]:
            body.append(f"  {escape(line)}\n", style="dim white")

        # Pinned cost line (separator + cost)
        if self._cost_line:
            body.append("\n  " + "─" * 36 + "\n", style="dim white")
            body.append(f"  {self._cost_line}\n", style="bold green")

        return Panel(
            body,
            title="[bold cyan] WEB IN THE SHELL [/bold cyan]",
            border_style="cyan",
            box=box.DOUBLE_EDGE,
        )

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

        return Panel(
            table,
            title="[bold magenta] NETWORK MONITOR [/bold magenta]",
            border_style="magenta",
            box=box.DOUBLE_EDGE,
        )

    def _refresh(self) -> None:
        if self._plain:
            return  # plain mode: output is immediate via print(); no refresh needed
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
        url = _redact(url)  # strip tokens / long cookie values from URL
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
        """Update running token totals and recompute the pinned cost line.

        Approximate cost is derived from *_COST_PER_1K*; unknown models are
        treated as zero cost (line still shows token counts).

        Args:
            tokens_in:  Number of input (prompt) tokens consumed in this call.
            tokens_out: Number of output (completion) tokens produced.
            model:      Model identifier string, e.g. ``"claude-sonnet-4-6"``.
        """
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
        """Update the step-progress indicator shown below the status badge.

        Args:
            step_num: Current step number (1-based).
            total:    Total number of steps in this run.
            label:    Short human-readable name for the current step,
                      e.g. ``"create_post"``.
        """
        self._step = (step_num, total, label)
        if self._plain:
            print(f"[STEP] {step_num}/{total}: {label}")
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

    # ------------------------------------------------------------------
    # Context manager
    # ------------------------------------------------------------------

    def __enter__(self) -> "AgentDisplay":
        if _NO_COLOR:
            self._plain = True
            # No Rich Live — all output goes to plain print()
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
