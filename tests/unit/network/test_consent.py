from unittest.mock import AsyncMock, MagicMock, patch


from network.intercept.consent import (
    _CAPTCHA_CHECKS,
    _CONSENT_SELECTORS,
    detect_captcha,
    dismiss_consent,
    dismiss_overlays,
)


def _make_page(*, selector_return=None, evaluate_return=None):
    page = MagicMock()
    page.query_selector = AsyncMock(return_value=selector_return)
    page.evaluate = AsyncMock(return_value=evaluate_return)
    return page


def _visible_el():
    el = AsyncMock()
    el.is_visible = AsyncMock(return_value=True)
    el.click = AsyncMock()
    return el


def _invisible_el():
    el = AsyncMock()
    el.is_visible = AsyncMock(return_value=False)
    el.click = AsyncMock()
    return el


# ── dismiss_consent ─────────────────────────────────────────────────────────


@patch("network.intercept.consent.asyncio.sleep", new_callable=AsyncMock)
async def test_dismiss_consent_hits_first_matching_selector(mock_sleep):
    el = _visible_el()
    page = _make_page(selector_return=el)

    result = await dismiss_consent(page)

    assert result is True
    el.click.assert_awaited_once()
    mock_sleep.assert_awaited_once_with(0.5)


@patch("network.intercept.consent.asyncio.sleep", new_callable=AsyncMock)
async def test_dismiss_consent_skips_invisible_element(mock_sleep):
    invisible = _invisible_el()
    visible = _visible_el()
    page = MagicMock()
    page.query_selector = AsyncMock(side_effect=[invisible, visible])
    page.evaluate = AsyncMock(return_value=False)

    result = await dismiss_consent(page)

    assert result is True
    invisible.click.assert_not_awaited()
    visible.click.assert_awaited_once()


@patch("network.intercept.consent.asyncio.sleep", new_callable=AsyncMock)
async def test_dismiss_consent_skips_none_element(mock_sleep):
    visible = _visible_el()
    nones = [None] * (len(_CONSENT_SELECTORS) - 1)
    page = MagicMock()
    page.query_selector = AsyncMock(side_effect=nones + [visible])

    result = await dismiss_consent(page)

    assert result is True
    visible.click.assert_awaited_once()


@patch("network.intercept.consent.asyncio.sleep", new_callable=AsyncMock)
async def test_dismiss_consent_continues_on_selector_exception(mock_sleep):
    visible = _visible_el()
    page = MagicMock()
    page.query_selector = AsyncMock(side_effect=[Exception("boom"), visible])

    result = await dismiss_consent(page)

    assert result is True
    visible.click.assert_awaited_once()


@patch("network.intercept.consent.asyncio.sleep", new_callable=AsyncMock)
async def test_dismiss_consent_js_fallback_when_no_selector_matches(mock_sleep):
    page = MagicMock()
    page.query_selector = AsyncMock(return_value=None)
    page.evaluate = AsyncMock(return_value=True)

    result = await dismiss_consent(page)

    assert result is True
    mock_sleep.assert_awaited_once_with(0.5)


@patch("network.intercept.consent.asyncio.sleep", new_callable=AsyncMock)
async def test_dismiss_consent_js_fallback_returns_false(mock_sleep):
    page = MagicMock()
    page.query_selector = AsyncMock(return_value=None)
    page.evaluate = AsyncMock(return_value=False)

    result = await dismiss_consent(page)

    assert result is False
    mock_sleep.assert_not_awaited()


@patch("network.intercept.consent.asyncio.sleep", new_callable=AsyncMock)
async def test_dismiss_consent_js_fallback_exception(mock_sleep):
    page = MagicMock()
    page.query_selector = AsyncMock(return_value=None)
    page.evaluate = AsyncMock(side_effect=Exception("js error"))

    result = await dismiss_consent(page)

    assert result is False
    mock_sleep.assert_not_awaited()


@patch("network.intercept.consent.asyncio.sleep", new_callable=AsyncMock)
async def test_dismiss_consent_returns_false_when_nothing_works(mock_sleep):
    page = MagicMock()
    page.query_selector = AsyncMock(return_value=None)
    page.evaluate = AsyncMock(return_value=None)

    result = await dismiss_consent(page)

    assert result is False


@patch("network.intercept.consent.asyncio.sleep", new_callable=AsyncMock)
async def test_dismiss_consent_sleeps_after_click(mock_sleep):
    el = _visible_el()
    page = _make_page(selector_return=el)

    await dismiss_consent(page)

    mock_sleep.assert_awaited_once_with(0.5)


async def test_dismiss_consent_all_known_selectors_covered():
    assert len(_CONSENT_SELECTORS) >= 15


# ── dismiss_overlays ─────────────────────────────────────────────────────────


@patch("network.intercept.consent.asyncio.sleep", new_callable=AsyncMock)
async def test_dismiss_overlays_clicks_first_visible(mock_sleep):
    el = _visible_el()
    page = _make_page(selector_return=el)

    result = await dismiss_overlays(page)

    assert result is True
    el.click.assert_awaited_once()
    mock_sleep.assert_awaited_once_with(0.3)


@patch("network.intercept.consent.asyncio.sleep", new_callable=AsyncMock)
async def test_dismiss_overlays_js_fallback(mock_sleep):
    page = MagicMock()
    page.query_selector = AsyncMock(return_value=None)
    page.evaluate = AsyncMock(return_value=True)

    result = await dismiss_overlays(page)

    assert result is True
    mock_sleep.assert_awaited_once_with(0.3)


@patch("network.intercept.consent.asyncio.sleep", new_callable=AsyncMock)
async def test_dismiss_overlays_js_exception_returns_false(mock_sleep):
    page = MagicMock()
    page.query_selector = AsyncMock(return_value=None)
    page.evaluate = AsyncMock(side_effect=Exception("js fail"))

    result = await dismiss_overlays(page)

    assert result is False
    mock_sleep.assert_not_awaited()


@patch("network.intercept.consent.asyncio.sleep", new_callable=AsyncMock)
async def test_dismiss_overlays_nothing_found(mock_sleep):
    page = MagicMock()
    page.query_selector = AsyncMock(return_value=None)
    page.evaluate = AsyncMock(return_value=False)

    result = await dismiss_overlays(page)

    assert result is False


@patch("network.intercept.consent.asyncio.sleep", new_callable=AsyncMock)
async def test_dismiss_overlays_continues_on_selector_exception(mock_sleep):
    visible = _visible_el()
    page = MagicMock()
    page.query_selector = AsyncMock(side_effect=[Exception("boom"), visible])

    result = await dismiss_overlays(page)

    assert result is True
    visible.click.assert_awaited_once()


@patch("network.intercept.consent.asyncio.sleep", new_callable=AsyncMock)
async def test_dismiss_overlays_sleeps_after_click(mock_sleep):
    el = _visible_el()
    page = _make_page(selector_return=el)

    await dismiss_overlays(page)

    mock_sleep.assert_awaited_once_with(0.3)


# ── detect_captcha ───────────────────────────────────────────────────────────


def _page_with_visible_selector(target_selector: str):
    """Returns a page where query_selector returns a visible element for target_selector only."""
    visible = _visible_el()

    async def _qs(sel):
        if sel == target_selector:
            return visible
        return None

    page = MagicMock()
    page.query_selector = AsyncMock(side_effect=_qs)
    return page


async def test_detect_captcha_hcaptcha():
    page = _page_with_visible_selector("iframe[src*='hcaptcha.com']")
    result = await detect_captcha(page)
    assert result == "hCaptcha"


async def test_detect_captcha_recaptcha():
    page = _page_with_visible_selector("iframe[src*='recaptcha/api']")
    result = await detect_captcha(page)
    assert result == "reCAPTCHA"


async def test_detect_captcha_cloudflare_turnstile():
    page = _page_with_visible_selector("iframe[src*='challenges.cloudflare.com']")
    result = await detect_captcha(page)
    assert result == "Cloudflare Turnstile"


async def test_detect_captcha_arkose_labs():
    page = _page_with_visible_selector("iframe[src*='funcaptcha']")
    result = await detect_captcha(page)
    assert result == "Arkose Labs"


async def test_detect_captcha_datadome():
    page = _page_with_visible_selector("iframe[src*='geo.captcha-delivery.com']")
    result = await detect_captcha(page)
    assert result == "DataDome"


async def test_detect_captcha_generic():
    generic_sel = "[id*='captcha']:not([id*='nocaptcha']):not([id*='recaptcha-anchor'])"
    page = _page_with_visible_selector(generic_sel)
    result = await detect_captcha(page)
    assert result == "generic CAPTCHA"


async def test_detect_captcha_none_when_clean():
    page = MagicMock()
    page.query_selector = AsyncMock(return_value=None)
    result = await detect_captcha(page)
    assert result is None


async def test_detect_captcha_invisible_element_skipped():
    invisible = _invisible_el()
    page = MagicMock()
    page.query_selector = AsyncMock(return_value=invisible)
    result = await detect_captcha(page)
    assert result is None


async def test_detect_captcha_exception_per_selector_continues():
    visible = _visible_el()
    page = MagicMock()
    page.query_selector = AsyncMock(
        side_effect=[Exception("fail"), visible]
    )
    # First check raises, second check returns visible element.
    # The second entry in _CAPTCHA_CHECKS is ("reCAPTCHA", "iframe[src*='recaptcha/api']").
    expected_name = _CAPTCHA_CHECKS[1][0]
    result = await detect_captcha(page)
    assert result == expected_name


async def test_detect_captcha_returns_first_match():
    # Two consecutive checks match; must return the one with lower index.
    first_visible = _visible_el()
    second_visible = _visible_el()

    call_count = 0

    async def _qs(_sel):
        nonlocal call_count
        call_count += 1
        if call_count == 1:
            return first_visible
        return second_visible

    page = MagicMock()
    page.query_selector = AsyncMock(side_effect=_qs)

    result = await detect_captcha(page)
    assert result == _CAPTCHA_CHECKS[0][0]
    # Only one query_selector call should have been made (returns on first match).
    assert call_count == 1
