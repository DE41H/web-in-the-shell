import os

import pytest


@pytest.mark.integration
@pytest.mark.skipif(
    not os.environ.get("RUN_BROWSER"),
    reason="set RUN_BROWSER=1 to enable; requires `playwright install chromium`",
)
async def test_stealth_browser_launches_and_loads_page():
    """Smoke test for StealthBrowser; requires chromium installed."""
    from network.security.stealth import StealthBrowser

    async with StealthBrowser(headless=True) as browser:
        page = await browser.new_page()
        response = await page.goto("https://example.com/", wait_until="domcontentloaded")
        assert response is not None
        assert response.status < 400
        content = await page.content()
        assert "Example Domain" in content
