"""Tests for _is_sensitive, _detect_forms, _find_submit, _submit_form,
and _prompt_and_fill_forms in src/main.py."""
from unittest.mock import AsyncMock, MagicMock, patch

from main import (
    _is_sensitive,
    _detect_forms,
    _find_submit,
    _submit_form,
    _prompt_and_fill_forms,
)


# ── Helpers ────────────────────────────────────────────────────────────────────

def _make_page(*, fields=None, submit_el=None):
    """Return a minimal async page mock."""
    page = MagicMock()
    page.evaluate = AsyncMock(return_value=fields or [])
    page.query_selector = AsyncMock(return_value=None)
    page.fill = AsyncMock()
    page.keyboard = MagicMock()
    page.keyboard.press = AsyncMock()
    return page


def _make_element(*, visible=True, click_raises=None):
    el = AsyncMock()
    el.is_visible = AsyncMock(return_value=visible)
    if click_raises:
        el.click = AsyncMock(side_effect=click_raises)
    else:
        el.click = AsyncMock()
    return el


def _make_display(*, has_live=False):
    display = MagicMock()
    display.log_thought = MagicMock()
    display._plain = True
    display._live = MagicMock() if has_live else None
    return display


def _make_store(saved=None):
    store = AsyncMock()
    store.get_all_for_domain = AsyncMock(return_value=saved or {})
    store.save = AsyncMock()
    return store


def _field(name, ftype="text", label=None, selector=None):
    return {
        "name": name,
        "type": ftype,
        "label": label or name,
        "required": False,
        "id": "",
        "selector": selector or f'[name="{name}"]',
    }


# ── _is_sensitive ──────────────────────────────────────────────────────────────

def test_is_sensitive_password_type():
    assert _is_sensitive("password", "field") is True


def test_is_sensitive_password_name():
    assert _is_sensitive("text", "password") is True


def test_is_sensitive_passwd_name():
    assert _is_sensitive("text", "passwd") is True


def test_is_sensitive_pwd_name():
    assert _is_sensitive("text", "pwd") is True


def test_is_sensitive_otp_name():
    assert _is_sensitive("text", "otp_code") is True


def test_is_sensitive_pin_name():
    assert _is_sensitive("text", "user_pin") is True


def test_is_sensitive_cvv_name():
    assert _is_sensitive("text", "cvv") is True


def test_is_sensitive_secret_name():
    assert _is_sensitive("text", "api_secret") is True


def test_is_sensitive_ssn_name():
    assert _is_sensitive("text", "ssn") is True


def test_is_sensitive_card_num():
    assert _is_sensitive("text", "card_number") is True


def test_is_sensitive_card_num_no_separator():
    assert _is_sensitive("text", "cardnum") is True


def test_is_sensitive_normal_username():
    assert _is_sensitive("text", "username") is False


def test_is_sensitive_normal_email():
    assert _is_sensitive("email", "email") is False


def test_is_sensitive_case_insensitive():
    assert _is_sensitive("text", "PASSWORD") is True
    assert _is_sensitive("text", "OTP") is True


# ── _detect_forms ──────────────────────────────────────────────────────────────

async def test_detect_forms_builds_id_selector():
    page = _make_page(fields=[
        {"name": "usr", "type": "text", "label": "User", "required": False, "id": "user-id"}
    ])
    result = await _detect_forms(page)
    assert result[0]["selector"] == "#user-id"


async def test_detect_forms_builds_name_selector_when_no_id():
    page = _make_page(fields=[
        {"name": "email", "type": "email", "label": "Email", "required": False, "id": ""}
    ])
    result = await _detect_forms(page)
    assert result[0]["selector"] == '[name="email"]'


async def test_detect_forms_escapes_quotes_in_name():
    page = _make_page(fields=[
        {"name": 'fi"eld', "type": "text", "label": "F", "required": False, "id": ""}
    ])
    result = await _detect_forms(page)
    assert '\\"' in result[0]["selector"]


async def test_detect_forms_skips_field_without_id_or_name():
    page = _make_page(fields=[
        {"name": "", "type": "text", "label": "", "required": False, "id": ""}
    ])
    result = await _detect_forms(page)
    assert result == []


async def test_detect_forms_returns_empty_on_evaluate_exception():
    page = _make_page()
    page.evaluate = AsyncMock(side_effect=Exception("JS error"))
    result = await _detect_forms(page)
    assert result == []


async def test_detect_forms_returns_all_valid_fields():
    page = _make_page(fields=[
        {"name": "a", "type": "text", "label": "A", "required": False, "id": "a"},
        {"name": "b", "type": "email", "label": "B", "required": True, "id": ""},
        {"name": "", "type": "text", "label": "", "required": False, "id": ""},  # skipped
    ])
    result = await _detect_forms(page)
    assert len(result) == 2


# ── _find_submit ──────────────────────────────────────────────────────────────

async def test_find_submit_returns_type_submit_button():
    el = _make_element(visible=True)
    page = _make_page()
    page.query_selector = AsyncMock(return_value=el)
    result = await _find_submit(page)
    assert result is el


async def test_find_submit_skips_invisible_type_submit():
    invisible = _make_element(visible=False)
    visible = _make_element(visible=True)

    call_count = 0

    async def _qs(sel):
        nonlocal call_count
        call_count += 1
        if call_count == 1:
            return invisible
        return visible

    page = _make_page()
    page.query_selector = _qs
    result = await _find_submit(page)
    assert result is visible


async def test_find_submit_returns_none_when_nothing_found():
    page = _make_page()
    page.query_selector = AsyncMock(return_value=None)
    result = await _find_submit(page)
    assert result is None


async def test_find_submit_continues_on_query_selector_exception():
    el = _make_element(visible=True)
    calls = []

    async def _qs(sel):
        calls.append(sel)
        if len(calls) == 1:
            raise Exception("playwright error")
        return el

    page = _make_page()
    page.query_selector = _qs
    result = await _find_submit(page)
    assert result is el


async def test_find_submit_text_fallback_sign_in():
    # All type=submit queries return None; has-text('Sign in') returns visible element
    el = _make_element(visible=True)

    async def _qs(sel):
        if "Sign in" in sel:
            return el
        return None

    page = _make_page()
    page.query_selector = _qs
    result = await _find_submit(page)
    assert result is el


async def test_find_submit_text_fallback_submit_text():
    el = _make_element(visible=True)

    async def _qs(sel):
        if "Submit" in sel:
            return el
        return None

    page = _make_page()
    page.query_selector = _qs
    result = await _find_submit(page)
    assert result is el


async def test_find_submit_text_fallback_continue_text():
    el = _make_element(visible=True)

    async def _qs(sel):
        if "Continue" in sel:
            return el
        return None

    page = _make_page()
    page.query_selector = _qs
    result = await _find_submit(page)
    assert result is el


async def test_find_submit_text_fallback_log_in():
    el = _make_element(visible=True)

    async def _qs(sel):
        if "Log in" in sel:
            return el
        return None

    page = _make_page()
    page.query_selector = _qs
    result = await _find_submit(page)
    assert result is el


async def test_find_submit_text_fallback_invisible_skipped():
    invisible = _make_element(visible=False)

    async def _qs(sel):
        if "has-text" in sel:
            return invisible
        return None

    page = _make_page()
    page.query_selector = _qs
    result = await _find_submit(page)
    assert result is None


async def test_find_submit_text_fallback_exception_continues():
    el = _make_element(visible=True)
    calls = []

    async def _qs(sel):
        if "has-text" in sel:
            calls.append(sel)
            if len(calls) == 1:
                raise Exception("oops")
            return el
        return None

    page = _make_page()
    page.query_selector = _qs
    result = await _find_submit(page)
    assert result is el


async def test_find_submit_prefers_type_submit_over_text():
    type_submit = _make_element(visible=True)
    text_btn = _make_element(visible=True)

    async def _qs(sel):
        if "type" in sel:
            return type_submit
        return text_btn

    page = _make_page()
    page.query_selector = _qs
    result = await _find_submit(page)
    assert result is type_submit


# ── _submit_form ──────────────────────────────────────────────────────────────

async def test_submit_form_clicks_found_button():
    el = _make_element()
    page = _make_page()
    display = _make_display()

    with patch("main._find_submit", new=AsyncMock(return_value=el)):
        result = await _submit_form(page, display)

    el.click.assert_awaited_once()
    assert result is True


async def test_submit_form_presses_enter_when_no_button():
    page = _make_page()
    display = _make_display()

    with patch("main._find_submit", new=AsyncMock(return_value=None)):
        result = await _submit_form(page, display)

    page.keyboard.press.assert_awaited_once_with("Enter")
    assert result is True


async def test_submit_form_returns_false_on_click_exception():
    el = _make_element(click_raises=Exception("click failed"))
    page = _make_page()
    display = _make_display()

    with patch("main._find_submit", new=AsyncMock(return_value=el)):
        result = await _submit_form(page, display)

    assert result is False


async def test_submit_form_returns_false_on_keyboard_exception():
    page = _make_page()
    page.keyboard.press = AsyncMock(side_effect=Exception("keyboard error"))
    display = _make_display()

    with patch("main._find_submit", new=AsyncMock(return_value=None)):
        result = await _submit_form(page, display)

    assert result is False


async def test_submit_form_logs_submitting_on_click():
    el = _make_element()
    page = _make_page()
    display = _make_display()

    with patch("main._find_submit", new=AsyncMock(return_value=el)):
        await _submit_form(page, display)

    logged = " ".join(str(c) for c in display.log_thought.call_args_list)
    assert "Submitting" in logged or "ubmit" in logged


async def test_submit_form_logs_enter_fallback():
    page = _make_page()
    display = _make_display()

    with patch("main._find_submit", new=AsyncMock(return_value=None)):
        await _submit_form(page, display)

    logged = " ".join(str(c) for c in display.log_thought.call_args_list)
    assert "Enter" in logged


# ── _prompt_and_fill_forms ────────────────────────────────────────────────────

def _make_fields(*names, ftype="text"):
    return [_field(n, ftype=ftype) for n in names]


async def test_prompt_no_fields_returns_false():
    page = _make_page()
    store = _make_store()
    display = _make_display()

    with patch("main._detect_forms", new=AsyncMock(return_value=[])):
        result = await _prompt_and_fill_forms(page, "example.com", store, display)

    assert result is False
    store.get_all_for_domain.assert_not_awaited()


async def test_prompt_fully_autonomous_all_in_store():
    fields = _make_fields("username", "email")
    page = _make_page()
    store = _make_store(saved={"username": "alice", "email": "a@b.com"})
    display = _make_display()

    with (
        patch("main._detect_forms", new=AsyncMock(return_value=fields)),
        patch("main._submit_form", new=AsyncMock(return_value=True)) as mock_submit,
    ):
        result = await _prompt_and_fill_forms(page, "example.com", store, display)

    assert result is True
    assert page.fill.await_count == 2
    mock_submit.assert_awaited_once()


async def test_prompt_autonomous_fills_only_store_fields():
    """Only fields present in the store are filled; missing ones are skipped."""
    fields = _make_fields("username", "email", "phone")
    store = _make_store(saved={"username": "alice"})  # only one in store
    page = _make_page()
    display = _make_display()

    with (
        patch("main._detect_forms", new=AsyncMock(return_value=fields)),
        patch("main._submit_form", new=AsyncMock(return_value=True)),
    ):
        result = await _prompt_and_fill_forms(page, "example.com", store, display)

    assert result is True
    assert page.fill.await_count == 1
    page.fill.assert_awaited_once_with('[name="username"]', "alice")


async def test_prompt_no_sensitive_nothing_in_store_returns_false():
    fields = _make_fields("username", "email")
    store = _make_store(saved={})
    page = _make_page()
    display = _make_display()

    with patch("main._detect_forms", new=AsyncMock(return_value=fields)):
        result = await _prompt_and_fill_forms(page, "example.com", store, display)

    assert result is False
    page.fill.assert_not_awaited()


async def test_prompt_fill_exception_continues_to_next_field():
    fields = _make_fields("username", "email")
    store = _make_store(saved={"username": "alice", "email": "a@b.com"})
    page = _make_page()
    fill_calls = []

    async def _fill(sel, val):
        fill_calls.append((sel, val))
        if "username" in sel:
            raise Exception("fill failed")

    page.fill = _fill
    display = _make_display()

    with (
        patch("main._detect_forms", new=AsyncMock(return_value=fields)),
        patch("main._submit_form", new=AsyncMock(return_value=True)),
    ):
        result = await _prompt_and_fill_forms(page, "example.com", store, display)

    # Both fill attempts made despite first exception
    assert len(fill_calls) == 2
    assert result is True


async def test_prompt_no_interactive_fills_from_store_with_sensitive_field():
    """no_interactive=True: fill non-sensitive field from store, skip sensitive."""
    fields = [
        _field("username"),
        _field("password", ftype="password"),
    ]
    store = _make_store(saved={"username": "alice"})
    page = _make_page()
    display = _make_display()

    with (
        patch("main._detect_forms", new=AsyncMock(return_value=fields)),
        patch("main._submit_form", new=AsyncMock(return_value=True)) as mock_submit,
    ):
        result = await _prompt_and_fill_forms(
            page, "example.com", store, display, no_interactive=True
        )

    assert result is True
    page.fill.assert_awaited_once_with('[name="username"]', "alice")
    mock_submit.assert_awaited_once()


async def test_prompt_no_interactive_no_store_returns_false():
    fields = [_field("password", ftype="password")]
    store = _make_store(saved={})
    page = _make_page()
    display = _make_display()

    with patch("main._detect_forms", new=AsyncMock(return_value=fields)):
        result = await _prompt_and_fill_forms(
            page, "example.com", store, display, no_interactive=True
        )

    assert result is False
    page.fill.assert_not_awaited()


async def test_prompt_no_interactive_only_sensitive_nothing_in_store():
    fields = [_field("otp", ftype="text")]  # sensitive by name
    store = _make_store(saved={})
    page = _make_page()
    display = _make_display()

    with patch("main._detect_forms", new=AsyncMock(return_value=fields)):
        result = await _prompt_and_fill_forms(
            page, "example.com", store, display, no_interactive=True
        )

    assert result is False


async def test_prompt_interactive_sensitive_via_getpass():
    fields = [_field("password", ftype="password")]
    store = _make_store(saved={})
    page = _make_page()
    display = _make_display()

    with (
        patch("main._detect_forms", new=AsyncMock(return_value=fields)),
        patch("main._submit_form", new=AsyncMock(return_value=True)) as mock_submit,
        patch("main.getpass.getpass", return_value="s3cr3t"),
    ):
        result = await _prompt_and_fill_forms(page, "example.com", store, display)

    page.fill.assert_awaited_once_with('[name="password"]', "s3cr3t")
    mock_submit.assert_awaited_once()
    assert result is True


async def test_prompt_interactive_sensitive_getpass_eof_skips_field():
    fields = [_field("password", ftype="password")]
    store = _make_store(saved={})
    page = _make_page()
    display = _make_display()

    with (
        patch("main._detect_forms", new=AsyncMock(return_value=fields)),
        patch("main.getpass.getpass", side_effect=EOFError),
    ):
        result = await _prompt_and_fill_forms(page, "example.com", store, display)

    assert result is False
    page.fill.assert_not_awaited()


async def test_prompt_interactive_sensitive_keyboard_interrupt_skips():
    fields = [_field("password", ftype="password")]
    store = _make_store(saved={})
    page = _make_page()
    display = _make_display()

    with (
        patch("main._detect_forms", new=AsyncMock(return_value=fields)),
        patch("main.getpass.getpass", side_effect=KeyboardInterrupt),
    ):
        result = await _prompt_and_fill_forms(page, "example.com", store, display)

    assert result is False


async def test_prompt_interactive_saved_field_kept_on_empty_input():
    # Need a sensitive field to enter the interactive branch; email is non-sensitive
    # but has a saved value → the override prompt appears inside the interactive loop.
    fields = [_field("password", ftype="password"), _field("email")]
    store = _make_store(saved={"email": "saved@example.com"})
    page = _make_page()
    display = _make_display()

    with (
        patch("main._detect_forms", new=AsyncMock(return_value=fields)),
        patch("main._submit_form", new=AsyncMock(return_value=True)),
        patch("main.getpass.getpass", return_value="pass123"),
        patch("main._console") as mock_console,
    ):
        mock_console.input = MagicMock(return_value="")  # user hits Enter → keep saved
        result = await _prompt_and_fill_forms(page, "example.com", store, display)

    # password filled, saved email value used (not empty string)
    assert result is True
    calls = {call_args.args[0]: call_args.args[1] for call_args in page.fill.await_args_list}
    assert calls.get('[name="email"]') == "saved@example.com"


async def test_prompt_interactive_saved_field_overridden_by_user():
    # Same: need a sensitive field to trigger the interactive branch.
    fields = [_field("password", ftype="password"), _field("email")]
    store = _make_store(saved={"email": "old@example.com"})
    page = _make_page()
    display = _make_display()

    with (
        patch("main._detect_forms", new=AsyncMock(return_value=fields)),
        patch("main._submit_form", new=AsyncMock(return_value=True)),
        patch("main.getpass.getpass", return_value="pass123"),
        patch("main._console") as mock_console,
    ):
        mock_console.input = MagicMock(return_value="new@example.com")
        result = await _prompt_and_fill_forms(page, "example.com", store, display)

    assert result is True
    calls = {call_args.args[0]: call_args.args[1] for call_args in page.fill.await_args_list}
    assert calls.get('[name="email"]') == "new@example.com"
    store.save.assert_awaited_once_with("example.com", "email", "text", "new@example.com")


async def test_prompt_interactive_console_input_eof_uses_saved():
    fields = [_field("password", ftype="password"), _field("email")]
    store = _make_store(saved={"email": "saved@example.com"})
    page = _make_page()
    display = _make_display()

    with (
        patch("main._detect_forms", new=AsyncMock(return_value=fields)),
        patch("main._submit_form", new=AsyncMock(return_value=True)),
        patch("main.getpass.getpass", return_value="pass123"),
        patch("main._console") as mock_console,
    ):
        mock_console.input = MagicMock(side_effect=EOFError)
        result = await _prompt_and_fill_forms(page, "example.com", store, display)

    assert result is True
    calls = {call_args.args[0]: call_args.args[1] for call_args in page.fill.await_args_list}
    assert calls.get('[name="email"]') == "saved@example.com"


async def test_prompt_returns_false_if_all_values_empty():
    """Sensitive field where user provides empty string → nothing to fill."""
    fields = [_field("otp")]  # sensitive by name
    store = _make_store(saved={})
    page = _make_page()
    display = _make_display()

    with (
        patch("main._detect_forms", new=AsyncMock(return_value=fields)),
        patch("main.getpass.getpass", return_value=""),
    ):
        result = await _prompt_and_fill_forms(page, "example.com", store, display)

    assert result is False
    page.fill.assert_not_awaited()


async def test_prompt_live_paused_and_restarted_during_interactive():
    """Rich Live is stopped before prompts and restarted in finally."""
    fields = [_field("password", ftype="password")]
    store = _make_store(saved={})
    page = _make_page()
    display = _make_display(has_live=True)
    display._plain = False  # color mode so live is actually paused

    with (
        patch("main._detect_forms", new=AsyncMock(return_value=fields)),
        patch("main._submit_form", new=AsyncMock(return_value=True)),
        patch("main.getpass.getpass", return_value="pass123"),
    ):
        await _prompt_and_fill_forms(page, "example.com", store, display)

    display._live.stop.assert_called_once()
    display._live.start.assert_called_once()


async def test_prompt_live_not_paused_in_plain_mode():
    """Plain mode (_plain=True) skips the live pause even if _live is set."""
    fields = [_field("password", ftype="password")]
    store = _make_store(saved={})
    page = _make_page()
    display = _make_display(has_live=True)
    display._plain = True  # plain mode — don't pause

    with (
        patch("main._detect_forms", new=AsyncMock(return_value=fields)),
        patch("main._submit_form", new=AsyncMock(return_value=True)),
        patch("main.getpass.getpass", return_value="pass123"),
    ):
        await _prompt_and_fill_forms(page, "example.com", store, display)

    display._live.stop.assert_not_called()


async def test_prompt_submit_called_after_all_fills():
    """_submit_form is called after every page.fill, not before."""
    fields = _make_fields("a", "b")
    store = _make_store(saved={"a": "x", "b": "y"})
    page = _make_page()
    display = _make_display()
    call_order = []

    async def _fill(sel, val):
        call_order.append(("fill", sel))

    page.fill = _fill

    async def _submit(p, d):
        call_order.append(("submit",))
        return True

    with (
        patch("main._detect_forms", new=AsyncMock(return_value=fields)),
        patch("main._submit_form", new=_submit),
    ):
        await _prompt_and_fill_forms(page, "example.com", store, display)

    fill_indices = [i for i, c in enumerate(call_order) if c[0] == "fill"]
    submit_index = next(i for i, c in enumerate(call_order) if c[0] == "submit")
    assert all(fi < submit_index for fi in fill_indices)


async def test_prompt_no_interactive_sensitive_in_store_is_filled():
    """In no_interactive mode the store value is used regardless of field sensitivity."""
    fields = [
        _field("username"),
        _field("password", ftype="password"),  # sensitive — but store has it
    ]
    store = _make_store(saved={"username": "alice", "password": "hunter2"})
    page = _make_page()
    display = _make_display()

    with (
        patch("main._detect_forms", new=AsyncMock(return_value=fields)),
        patch("main._submit_form", new=AsyncMock(return_value=True)),
    ):
        result = await _prompt_and_fill_forms(
            page, "example.com", store, display, no_interactive=True
        )

    # Both fields are in the store, so both should be filled
    assert result is True
    assert page.fill.await_count == 2


async def test_prompt_domain_passed_to_store():
    fields = _make_fields("email")
    store = _make_store(saved={"email": "a@b.com"})
    page = _make_page()
    display = _make_display()

    with (
        patch("main._detect_forms", new=AsyncMock(return_value=fields)),
        patch("main._submit_form", new=AsyncMock(return_value=True)),
    ):
        await _prompt_and_fill_forms(page, "my.domain.com", store, display)

    store.get_all_for_domain.assert_awaited_once_with("my.domain.com")
