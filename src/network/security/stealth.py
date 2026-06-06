import asyncio
import sys

from playwright.async_api import async_playwright, Browser, BrowserContext, Page, Playwright
from playwright_stealth import Stealth

_stealth = Stealth()


class StealthBrowser:
    """Headless Chromium hardened against bot-detection fingerprinting."""

    def __init__(self, headless: bool = True) -> None:
        self.headless = headless
        self._playwright: Playwright | None = None
        self._browser: Browser | None = None
        self._context: BrowserContext | None = None

    async def __aenter__(self) -> "StealthBrowser":
        self._playwright = await async_playwright().start()
        self._browser = await self._playwright.chromium.launch(
            headless=self.headless,
            args=[
                "--no-sandbox",
                "--disable-blink-features=AutomationControlled",
                "--disable-dev-shm-usage",
                "--disable-extensions",
                "--disable-setuid-sandbox",
            ],
        )
        self._context = await self._browser.new_context(
            user_agent=(
                "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                "AppleWebKit/537.36 (KHTML, like Gecko) "
                "Chrome/124.0.0.0 Safari/537.36"
            ),
            viewport={"width": 1920, "height": 1080},
            bypass_csp=True,
        )
        return self

    async def __aexit__(self, *_: object) -> None:
        if self._context:
            await self._context.close()
        if self._browser:
            await self._browser.close()
        if self._playwright:
            await self._playwright.stop()

    async def new_page(self) -> Page:
        if self._context is None:
            raise RuntimeError("Browser not launched. Use 'async with StealthBrowser()' first.")
        page = await self._context.new_page()
        await _stealth.apply_stealth_async(page)
        return page

    async def login_handshake(self, url: str) -> Page:
        """Open a visible browser window for a human to complete a login flow.

        Some sites block headless auth entirely. This method launches a second
        browser in non-headless mode (regardless of how *this* instance was
        started), navigates to `url`, and waits for the user to press Enter
        after completing login. The resulting page — with its authenticated
        cookies already set on the context — is returned so the caller can
        attach SessionManager and PacketSniffer to it.

        The wait for Enter is performed via run_in_executor so the event loop
        stays unblocked while the human interacts with the browser.
        """
        if self._playwright is None:
            raise RuntimeError("Browser not launched. Use 'async with StealthBrowser()' first.")

        # Always launch the login browser in non-headless (visible) mode so the
        # human can actually interact with it.
        login_browser: Browser = await self._playwright.chromium.launch(
            headless=False,
            args=[
                "--no-sandbox",
                "--disable-blink-features=AutomationControlled",
                "--disable-dev-shm-usage",
                "--disable-extensions",
                "--disable-setuid-sandbox",
            ],
        )
        login_context = await login_browser.new_context(
            user_agent=(
                "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                "AppleWebKit/537.36 (KHTML, like Gecko) "
                "Chrome/124.0.0.0 Safari/537.36"
            ),
            viewport={"width": 1920, "height": 1080},
            bypass_csp=True,
        )
        page = await login_context.new_page()
        await _stealth.apply_stealth_async(page)
        await page.goto(url)

        print(
            f"[login] Navigate to {url} in the browser window, "
            "complete login, then press Enter here...",
            file=sys.stderr,
            flush=True,
        )

        loop = asyncio.get_event_loop()
        await loop.run_in_executor(None, input)

        # Trigger a cookie snapshot so callers can inspect session state
        # immediately after the handshake if needed. The actual cookies live on
        # the context and are accessible via page.context.cookies().
        await page.context.cookies()

        return page
