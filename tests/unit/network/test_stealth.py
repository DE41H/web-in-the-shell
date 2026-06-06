from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from network.security.stealth import StealthBrowser


def _make_playwright_stack(*, headless: bool = True):
    pw_instance = MagicMock()
    pw_instance.start = AsyncMock(return_value=pw_instance)
    pw_instance.stop = AsyncMock()

    browser = AsyncMock()
    pw_instance.chromium.launch = AsyncMock(return_value=browser)

    context = AsyncMock()
    browser.new_context = AsyncMock(return_value=context)

    page = AsyncMock()
    context.new_page = AsyncMock(return_value=page)

    return pw_instance, browser, context, page


def _patched_async_playwright(mock_async_playwright, pw_instance):
    mock_async_playwright.return_value = AsyncMock()
    mock_async_playwright.return_value.start = AsyncMock(return_value=pw_instance)
    mock_async_playwright.return_value.chromium = pw_instance.chromium


@patch("network.security.stealth.async_playwright")
async def test_launch_sets_browser_and_context(mock_async_playwright):
    pw_instance, browser, context, _ = _make_playwright_stack()
    _patched_async_playwright(mock_async_playwright, pw_instance)

    sb = StealthBrowser(headless=True)
    await sb.__aenter__()

    assert sb._browser is browser
    assert sb._context is context


@patch("network.security.stealth.async_playwright")
async def test_launch_passes_headless_true(mock_async_playwright):
    pw_instance, _, _, _ = _make_playwright_stack()
    _patched_async_playwright(mock_async_playwright, pw_instance)

    sb = StealthBrowser(headless=True)
    await sb.__aenter__()

    call = pw_instance.chromium.launch.call_args
    assert call.kwargs["headless"] is True


@patch("network.security.stealth.async_playwright")
async def test_launch_passes_headless_false(mock_async_playwright):
    pw_instance, _, _, _ = _make_playwright_stack()
    _patched_async_playwright(mock_async_playwright, pw_instance)

    sb = StealthBrowser(headless=False)
    await sb.__aenter__()

    call = pw_instance.chromium.launch.call_args
    assert call.kwargs["headless"] is False


@patch("network.security.stealth.async_playwright")
async def test_launch_includes_stealth_args(mock_async_playwright):
    pw_instance, _, _, _ = _make_playwright_stack()
    _patched_async_playwright(mock_async_playwright, pw_instance)

    sb = StealthBrowser()
    await sb.__aenter__()

    args = pw_instance.chromium.launch.call_args.kwargs["args"]
    expected = {
        "--no-sandbox",
        "--disable-blink-features=AutomationControlled",
        "--disable-dev-shm-usage",
        "--disable-extensions",
        "--disable-setuid-sandbox",
    }
    for flag in expected:
        assert flag in args


@patch("network.security.stealth.async_playwright")
async def test_new_context_sets_user_agent(mock_async_playwright):
    pw_instance, browser, _, _ = _make_playwright_stack()
    _patched_async_playwright(mock_async_playwright, pw_instance)

    sb = StealthBrowser()
    await sb.__aenter__()

    ua = browser.new_context.call_args.kwargs["user_agent"]
    assert "Mozilla/5.0" in ua
    assert "Chrome" in ua


@patch("network.security.stealth.async_playwright")
async def test_new_context_sets_viewport(mock_async_playwright):
    pw_instance, browser, _, _ = _make_playwright_stack()
    _patched_async_playwright(mock_async_playwright, pw_instance)

    sb = StealthBrowser()
    await sb.__aenter__()

    viewport = browser.new_context.call_args.kwargs["viewport"]
    assert viewport == {"width": 1920, "height": 1080}


@patch("network.security.stealth.async_playwright")
async def test_new_context_bypass_csp(mock_async_playwright):
    pw_instance, browser, _, _ = _make_playwright_stack()
    _patched_async_playwright(mock_async_playwright, pw_instance)

    sb = StealthBrowser()
    await sb.__aenter__()

    assert browser.new_context.call_args.kwargs["bypass_csp"] is True


@patch("network.security.stealth._stealth")
@patch("network.security.stealth.async_playwright")
async def test_new_page_applies_stealth(mock_async_playwright, mock_stealth):
    pw_instance, _, _, page = _make_playwright_stack()
    _patched_async_playwright(mock_async_playwright, pw_instance)
    mock_stealth.apply_stealth_async = AsyncMock()

    sb = StealthBrowser()
    await sb.__aenter__()
    returned = await sb.new_page()

    mock_stealth.apply_stealth_async.assert_awaited_once_with(page)
    assert returned is page


async def test_new_page_before_launch_raises():
    sb = StealthBrowser()
    with pytest.raises(RuntimeError):
        await sb.new_page()


@patch("network.security.stealth.async_playwright")
async def test_context_manager_lifecycle(mock_async_playwright):
    pw_instance, browser, context, _ = _make_playwright_stack()
    _patched_async_playwright(mock_async_playwright, pw_instance)

    sb = StealthBrowser()
    async with sb:
        assert sb._browser is browser
        assert sb._context is context

    context.close.assert_awaited_once()
    browser.close.assert_awaited_once()
    pw_instance.stop.assert_awaited_once()


@patch("network.security.stealth.async_playwright")
async def test_close_calls_all_three_close_methods(mock_async_playwright):
    pw_instance, browser, context, _ = _make_playwright_stack()
    _patched_async_playwright(mock_async_playwright, pw_instance)

    sb = StealthBrowser()
    await sb.__aenter__()
    await sb.__aexit__(None, None, None)

    context.close.assert_awaited_once()
    browser.close.assert_awaited_once()
    pw_instance.stop.assert_awaited_once()


async def test_close_idempotent_on_uninitialized():
    sb = StealthBrowser()
    await sb.__aexit__(None, None, None)
    assert sb._context is None
    assert sb._browser is None
    assert sb._playwright is None


# ---- login_handshake ----

async def test_login_handshake_before_launch_raises():
    sb = StealthBrowser()
    with pytest.raises(RuntimeError):
        await sb.login_handshake("https://example.com/login")


@patch("network.security.stealth._stealth")
@patch("network.security.stealth.asyncio.get_event_loop")
@patch("network.security.stealth.async_playwright")
async def test_login_handshake_launches_visible_browser(
    mock_async_playwright, mock_get_event_loop, mock_stealth
):
    pw_instance, browser, context, _ = _make_playwright_stack()

    login_browser = AsyncMock()
    login_context = AsyncMock()
    login_page = AsyncMock()
    login_browser.new_context = AsyncMock(return_value=login_context)
    login_context.new_page = AsyncMock(return_value=login_page)
    login_page.context.cookies = AsyncMock(return_value=[])

    pw_instance.chromium.launch = AsyncMock(side_effect=[browser, login_browser])

    _patched_async_playwright(mock_async_playwright, pw_instance)

    mock_loop = MagicMock()
    mock_loop.run_in_executor = AsyncMock(return_value=None)
    mock_get_event_loop.return_value = mock_loop

    mock_stealth.apply_stealth_async = AsyncMock()

    sb = StealthBrowser()
    await sb.__aenter__()
    returned = await sb.login_handshake("https://example.com/login")

    assert returned is login_page
    launch_calls = pw_instance.chromium.launch.call_args_list
    assert launch_calls[0].kwargs["headless"] is True
    assert launch_calls[1].kwargs["headless"] is False
    login_page.goto.assert_awaited_once_with("https://example.com/login")
    mock_stealth.apply_stealth_async.assert_any_await(login_page)
