import asyncio
import re
from unittest.mock import AsyncMock, patch

from rich.text import Text
from tui.display import (
    AgentDisplay,
    _COST_PER_1K,
    _STATUS_STYLE,
    _redact,
)


# ---- Module-level constants ----

def test_cost_table_has_sonnet():
    assert "claude-sonnet-4-6" in _COST_PER_1K
    assert _COST_PER_1K["claude-sonnet-4-6"] == (0.003, 0.015)


def test_cost_table_has_haiku():
    assert "claude-haiku-4-5-20251001" in _COST_PER_1K
    assert _COST_PER_1K["claude-haiku-4-5-20251001"] == (0.00025, 0.00125)


def test_status_style_known_states():
    for state in ("Idle", "Planning", "Executing", "Recovering", "Complete"):
        assert state in _STATUS_STYLE
        assert isinstance(_STATUS_STYLE[state], str)
        assert _STATUS_STYLE[state]


# ---- Redaction ----

def test_redact_bearer():
    assert _redact("Authorization: Bearer abc123def456ghi789") == "Authorization: Bearer [REDACTED]"


def test_redact_preserves_short_bearer():
    assert _redact("Bearer abc") == "Bearer abc"


def test_redact_cookie_value():
    assert _redact("sessionid=1234567890abcdefghijk") == "sessionid=[REDACTED]"


def test_redact_preserves_short_cookie():
    assert _redact("key=abc") == "key=abc"


def test_redact_multiple():
    text = "Bearer abc123def456ghi789 and sessionid=1234567890abcdefghijk"
    out = _redact(text)
    assert "Bearer [REDACTED]" in out
    assert "sessionid=[REDACTED]" in out
    assert "abc123def456ghi789" not in out
    assert "1234567890abcdefghijk" not in out


def test_redact_no_match():
    assert _redact("just some plain text with no secrets") == "just some plain text with no secrets"


def test_redact_empty():
    assert _redact("") == ""


# ---- AgentDisplay plain-mode ----

@patch("tui.display._NO_COLOR", True)
def test_set_status_plain_mode(capsys):
    d = AgentDisplay()
    with d:
        d.set_status("Planning")
    out = capsys.readouterr().out
    assert "[STATUS] Planning" in out


@patch("tui.display._NO_COLOR", True)
def test_set_status_initial_idle():
    d = AgentDisplay()
    assert d._status == "Idle"
    with d:
        d.set_status("Planning")
    assert d._status == "Planning"


@patch("tui.display._NO_COLOR", True)
def test_log_thought_appends_with_timestamp():
    d = AgentDisplay()
    with d:
        d.log_thought("hello world")
    assert len(d._thoughts) == 1
    assert re.match(r"^\[\d{2}:\d{2}:\d{2}\] hello world$", d._thoughts[0])


@patch("tui.display._NO_COLOR", True)
def test_log_thought_redacts_bearer(capsys):
    d = AgentDisplay()
    with d:
        d.log_thought("Got Bearer abc123def456ghi789 from server")
    out = capsys.readouterr().out
    assert "Bearer [REDACTED]" in out
    assert "abc123def456ghi789" not in out
    assert "Bearer [REDACTED]" in d._thoughts[0]
    assert "abc123def456ghi789" not in d._thoughts[0]


@patch("tui.display._NO_COLOR", True)
def test_log_thought_redacts_cookie():
    d = AgentDisplay()
    with d:
        d.log_thought("sessionid=1234567890abcdefghijk")
    assert "sessionid=[REDACTED]" in d._thoughts[0]
    assert "1234567890abcdefghijk" not in d._thoughts[0]


@patch("tui.display._NO_COLOR", True)
def test_log_intercept_redacts_url():
    d = AgentDisplay()
    with d:
        d.log_intercept(
            "https://api.example.com/?token=abcdefghijklmnop123",
            200,
            100,
            50,
        )
    assert d._intercepts[0]["url"] == "https://api.example.com/?token=[REDACTED]"


@patch("tui.display._NO_COLOR", True)
def test_log_intercept_appends_to_intercepts():
    d = AgentDisplay()
    with d:
        d.log_intercept("https://api.example.com/posts", 200, 1000, 500)
    assert len(d._intercepts) == 1
    cap = d._intercepts[0]
    assert cap["url"] == "https://api.example.com/posts"
    assert cap["status"] == 200
    assert cap["raw_bytes"] == 1000
    assert cap["compact_bytes"] == 500


@patch("tui.display._NO_COLOR", True)
def test_log_intercept_plain_mode_print(capsys):
    d = AgentDisplay()
    with d:
        d.log_intercept("https://api.example.com/posts", 200, 5000, 1000)
    out = capsys.readouterr().out
    assert "https://api.example.com/posts" in out
    assert "status=200" in out
    assert "raw=5,000b" in out
    assert "compact=1,000b" in out
    assert "ratio=" in out


@patch("tui.display._NO_COLOR", True)
def test_log_cost_unknown_model_zero_cost():
    d = AgentDisplay()
    with d:
        d.log_cost(1000, 500, "unknown-model")
    assert "$0.0000" in d._cost_line
    assert "1,000" in d._cost_line
    assert "500" in d._cost_line


@patch("tui.display._NO_COLOR", True)
def test_log_cost_sonnet_correct_math():
    d = AgentDisplay()
    with d:
        d.log_cost(1000, 500, "claude-sonnet-4-6")
    assert "$0.0105" in d._cost_line
    assert "1,000" in d._cost_line
    assert "500" in d._cost_line


@patch("tui.display._NO_COLOR", True)
def test_log_cost_haiku_correct_math():
    d = AgentDisplay()
    with d:
        d.log_cost(1_000_000, 500_000, "claude-haiku-4-5-20251001")
    assert "$0.8750" in d._cost_line


@patch("tui.display._NO_COLOR", True)
def test_log_cost_accumulates_totals():
    d = AgentDisplay()
    with d:
        d.log_cost(100, 50, "claude-sonnet-4-6")
        d.log_cost(100, 50, "claude-sonnet-4-6")
    assert d._total_tokens_in == 200
    assert d._total_tokens_out == 100


@patch("tui.display._NO_COLOR", True)
def test_log_step_updates_step_field():
    d = AgentDisplay()
    with d:
        d.log_step(2, 5, "create_post")
    assert d._step == (2, 5, "create_post")


@patch("tui.display._NO_COLOR", True)
def test_log_step_plain_mode_print(capsys):
    d = AgentDisplay()
    with d:
        d.log_step(2, 5, "create_post")
    out = capsys.readouterr().out
    assert "[STEP] 2/5: create_post" in out


# ---- AgentDisplay color-mode (mocked Live) ----

@patch("tui.display._NO_COLOR", False)
@patch("tui.display.Live")
def test_live_context_manager_instantiated(mock_live_class):
    d = AgentDisplay()
    with d:
        pass
    mock_live_class.assert_called_once()


@patch("tui.display._NO_COLOR", False)
@patch("tui.display.Live")
def test_live_context_manager_exited(mock_live_class):
    d = AgentDisplay()
    with d:
        pass
    mock_live_class.return_value.__exit__.assert_called()


@patch("tui.display._NO_COLOR", False)
@patch("tui.display.Live")
def test_status_style_color_codes_present(mock_live_class):
    d = AgentDisplay()
    with d:
        d.set_status("Planning")
    assert d._status == "Planning"


# ---- NO_COLOR switching at __enter__ ----

@patch("tui.display._NO_COLOR", True)
def test_enter_in_plain_mode_does_not_create_live():
    d = AgentDisplay()
    with d:
        assert d._plain is True
        assert d._live is None


@patch("tui.display._NO_COLOR", False)
@patch("tui.display.Live")
def test_no_color_false_creates_live(mock_live):
    d = AgentDisplay()
    with d:
        assert d._live is not None


# ---- AgentDisplay color-mode rendering paths (Wave 3B) ----


@patch("tui.display._NO_COLOR", False)
@patch("tui.display.Live")
def test_color_mode_log_thought_appends_with_timestamp(mock_live_class):
    d = AgentDisplay()
    with d:
        d.log_thought("hello world")
    assert len(d._thoughts) == 1
    assert re.match(r"^\[\d{2}:\d{2}:\d{2}\] hello world$", d._thoughts[0])
    mock_live_class.return_value.refresh.assert_called()


@patch("tui.display._NO_COLOR", False)
@patch("tui.display.Live")
def test_color_mode_log_intercept_calls_refresh(mock_live_class):
    d = AgentDisplay()
    with d:
        d.log_intercept("https://api.example.com/posts", 200, 1000, 500)
    mock_live_class.return_value.refresh.assert_called()


@patch("tui.display._NO_COLOR", False)
@patch("tui.display.Live")
def test_color_mode_log_intercept_appends_to_intercepts_list(mock_live_class):
    d = AgentDisplay()
    with d:
        d.log_intercept("https://api.example.com/posts", 200, 1000, 500)
    assert len(d._intercepts) == 1
    cap = d._intercepts[0]
    assert cap["url"] == "https://api.example.com/posts"
    assert cap["status"] == 200
    assert cap["raw_bytes"] == 1000
    assert cap["compact_bytes"] == 500


@patch("tui.display._NO_COLOR", False)
@patch("tui.display.Live")
def test_color_mode_log_cost_updates_pinned_line(mock_live_class):
    d = AgentDisplay()
    with d:
        d.log_cost(1000, 500, "claude-sonnet-4-6")
    assert d._cost_line
    assert "Cost:" in d._cost_line
    mock_live_class.return_value.refresh.assert_called()


@patch("tui.display._NO_COLOR", False)
@patch("tui.display.Live")
def test_color_mode_log_cost_unknown_model_zero_cost(mock_live_class):
    d = AgentDisplay()
    with d:
        d.log_cost(1000, 500, "unknown-model")
    assert "$0.0000" in d._cost_line
    assert "1,000" in d._cost_line
    assert "500" in d._cost_line


@patch("tui.display._NO_COLOR", False)
@patch("tui.display.Live")
def test_color_mode_log_cost_sonnet_correct_math(mock_live_class):
    d = AgentDisplay()
    with d:
        d.log_cost(1000, 500, "claude-sonnet-4-6")
    assert "$0.0105" in d._cost_line
    assert "1,000" in d._cost_line
    assert "500" in d._cost_line


@patch("tui.display._NO_COLOR", False)
@patch("tui.display.Live")
def test_color_mode_log_step_calls_refresh(mock_live_class):
    d = AgentDisplay()
    with d:
        d.log_step(2, 5, "create_post")
    assert d._step == (2, 5, "create_post")
    mock_live_class.return_value.refresh.assert_called()


@patch("tui.display._NO_COLOR", False)
@patch("tui.display.Live")
def test_color_mode_set_status_calls_refresh(mock_live_class):
    d = AgentDisplay()
    with d:
        d.set_status("Planning")
    assert d._status == "Planning"
    mock_live_class.return_value.refresh.assert_called()


@patch("tui.display._NO_COLOR", False)
@patch("tui.display.Live")
@patch("tui.display.asyncio.sleep", new_callable=AsyncMock)
async def test_color_mode_countdown_exit_decrements_status(mock_sleep, mock_live_class):
    d = AgentDisplay()
    captured: list[str] = []
    mock_live_class.return_value.refresh.side_effect = lambda: captured.append(d._status)
    with d:
        await d.countdown_exit(3)
    assert "Done  (3s)" in captured
    assert "Done  (2s)" in captured
    assert "Done  (1s)" in captured
    assert d._status == "Complete"


@patch("tui.display._NO_COLOR", False)
@patch("tui.display.Live")
@patch("tui.display.asyncio.sleep", new_callable=AsyncMock)
async def test_color_mode_countdown_exit_keyboard_interrupt_short_circuits(
    mock_sleep, mock_live_class
):
    d = AgentDisplay()
    mock_sleep.side_effect = KeyboardInterrupt
    with d:
        await d.countdown_exit(3)
    assert d._status != "Complete"


@patch("tui.display._NO_COLOR", False)
@patch("tui.display.Live")
@patch("tui.display.asyncio.sleep", new_callable=AsyncMock)
async def test_color_mode_countdown_exit_cancelled_error_short_circuits(
    mock_sleep, mock_live_class
):
    d = AgentDisplay()
    mock_sleep.side_effect = asyncio.CancelledError
    with d:
        await d.countdown_exit(3)
    assert d._status != "Complete"


@patch("tui.display._NO_COLOR", False)
@patch("tui.display.Live")
def test_color_mode_render_left_returns_panel(mock_live_class):
    from rich.panel import Panel
    d = AgentDisplay()
    with d:
        panel = d._render_left()
    assert isinstance(panel, Panel)


@patch("tui.display._NO_COLOR", False)
@patch("tui.display.Live")
def test_color_mode_render_right_returns_panel(mock_live_class):
    from rich.panel import Panel
    d = AgentDisplay()
    with d:
        panel = d._render_right()
    assert isinstance(panel, Panel)


@patch("tui.display._NO_COLOR", False)
@patch("tui.display.Live")
def test_color_mode_render_left_uses_thoughts_window(mock_live_class):
    d = AgentDisplay()
    with d:
        for i in range(25):
            d.log_thought(f"thought-{i}")
        panel = d._render_left()
        body = panel.renderable
        assert isinstance(body, Text)
        plain = body.plain
    assert "thought-24" in plain
    assert "thought-5" in plain
    assert "thought-4" not in plain
    assert "thought-0" not in plain


@patch("tui.display._NO_COLOR", False)
@patch("tui.display.Live")
def test_color_mode_render_right_handles_empty_intercepts(mock_live_class):
    from rich.panel import Panel
    d = AgentDisplay()
    with d:
        panel = d._render_right()
    assert isinstance(panel, Panel)
    assert d._intercepts == []


@patch("tui.display._NO_COLOR", False)
@patch("tui.display.Live")
def test_color_mode_log_intercept_redacts_url(mock_live_class):
    d = AgentDisplay()
    with d:
        d.log_intercept(
            "https://api.example.com/?token=abcdefghijklmnop123",
            200,
            100,
            50,
        )
    assert d._intercepts[0]["url"] == "https://api.example.com/?token=[REDACTED]"


# ---- Cost table completeness ----

def test_cost_table_has_opus():
    assert "claude-opus-4-8" in _COST_PER_1K
    # Opus is more expensive than sonnet
    assert _COST_PER_1K["claude-opus-4-8"][0] > _COST_PER_1K["claude-sonnet-4-6"][0]


def test_cost_table_has_gpt4o():
    assert "gpt-4o" in _COST_PER_1K
    assert _COST_PER_1K["gpt-4o"] == (0.0025, 0.010)


def test_cost_table_has_gpt4o_mini():
    assert "gpt-4o-mini" in _COST_PER_1K
    assert _COST_PER_1K["gpt-4o-mini"][0] < _COST_PER_1K["gpt-4o"][0]  # mini is cheaper


def test_cost_table_has_gemini_flash():
    assert "gemini-2.0-flash" in _COST_PER_1K


def test_cost_table_has_groq_llama():
    assert any(
        "llama" in k for k in _COST_PER_1K
    ), "Expected at least one llama model in cost table"


@patch("tui.display._NO_COLOR", True)
def test_cost_table_unknown_model_returns_zero_on_log():
    """log_cost with an unknown model should not crash and should show $0.0000."""
    d = AgentDisplay()
    with d:
        d.log_cost(100, 50, "unknown-model-xyz")
    assert "$0.0000" in d._cost_line


def test_cost_table_has_at_least_10_entries():
    assert len(_COST_PER_1K) >= 10


# ── Plain-mode branches ─────────────────────────────────────────────────────

@patch("tui.display._NO_COLOR", True)
def test_refresh_returns_immediately_in_plain_mode():
    """_refresh() in plain mode short-circuits — output is via print(), not Live."""
    d = AgentDisplay()
    with d:
        d._refresh()
    assert d._status == "Idle"


@patch("tui.display._NO_COLOR", True)
@patch("tui.display.asyncio.sleep", new_callable=AsyncMock)
async def test_plain_mode_countdown_exit_prints_ticks(mock_sleep, capsys):
    """countdown_exit in plain mode prints `[DONE] exiting in Ns...` per tick."""
    d = AgentDisplay()
    with d:
        await d.countdown_exit(3)
    out = capsys.readouterr().out
    assert "[DONE] exiting in 3s..." in out
    assert "[DONE] exiting in 2s..." in out
    assert "[DONE] exiting in 1s..." in out
    assert d._status == "Complete"
