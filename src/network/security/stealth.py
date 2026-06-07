import asyncio
import sys

from playwright.async_api import async_playwright, Browser, BrowserContext, Dialog, Page, Playwright
from playwright_stealth import Stealth

from network.dispatch.headers import (
    USER_AGENT as _USER_AGENT,
    SEC_CH_UA as _SEC_CH_UA,
    SEC_FETCH_HEADERS as _SEC_FETCH_HEADERS,
)

_stealth = Stealth()

_LAUNCH_ARGS = [
    "--no-sandbox",
    "--disable-blink-features=AutomationControlled",
    "--disable-dev-shm-usage",
    "--disable-extensions",
    "--disable-setuid-sandbox",
    # Reduce automation signals
    "--no-first-run",
    "--no-default-browser-check",
    "--disable-infobars",
    "--disable-ipc-flooding-protection",
    "--disable-renderer-backgrounding",
    "--disable-backgrounding-occluded-windows",
    "--disable-background-timer-throttling",
    "--disable-client-side-phishing-detection",
    "--disable-features=TranslateUI,IsolateOrigins",
    "--password-store=basic",
    "--use-mock-keychain",
    # Window size must match viewport to avoid outerWidth/innerWidth mismatch.
    "--window-size=1920,1080",
]

_CONTEXT_KWARGS = dict(
    user_agent=_USER_AGENT,
    viewport={"width": 1920, "height": 1080},
    bypass_csp=True,
    locale="en-US",
    timezone_id="America/New_York",
    extra_http_headers={
        "Accept-Language": "en-US,en;q=0.9",
        "sec-ch-ua": _SEC_CH_UA,
        "sec-ch-ua-mobile": "?0",
        "sec-ch-ua-platform": '"Windows"',
        **_SEC_FETCH_HEADERS,
    },
)

# JS patches that playwright_stealth doesn't cover.  Injected as an init
# script so they run before any page script, including anti-bot checks.
_INIT_SCRIPT = """
(() => {
  // hardware concurrency + device memory — common fingerprint values on
  // a mid-range Windows laptop; consistent with the declared UA.
  Object.defineProperty(navigator, 'hardwareConcurrency', {get: () => 8});
  Object.defineProperty(navigator, 'deviceMemory',       {get: () => 8});

  // navigator.connection — absent in headless but present in real Chrome.
  if (!navigator.connection) {
    Object.defineProperty(navigator, 'connection', {
      get: () => ({
        effectiveType: '4g',
        rtt: 50,
        downlink: 10,
        saveData: false,
      }),
    });
  }

  // Permissions API — return 'default' (not 'denied') for notifications so
  // the site can't distinguish headless from a real browser.
  const _origQuery = window.Permissions && window.Permissions.prototype.query;
  if (_origQuery) {
    window.Permissions.prototype.query = function(params) {
      if (params && params.name === 'notifications') {
        return Promise.resolve({state: 'default', onchange: null});
      }
      return _origQuery.call(this, params);
    };
  }

  // Ensure window.chrome is a non-null object (headless Chromium sometimes
  // leaves it undefined, which is a trivial bot signal).
  if (!window.chrome) {
    window.chrome = {runtime: {}};
  }
})();
"""


class StealthBrowser:
    """Headless Chromium hardened against bot-detection fingerprinting."""

    def __init__(self, headless: bool = True) -> None:
        self.headless = headless
        self._playwright: Playwright | None = None
        self._browser: Browser | None = None
        self._context: BrowserContext | None = None
        self._login_browser: Browser | None = None

    async def __aenter__(self) -> "StealthBrowser":
        self._playwright = await async_playwright().start()
        self._browser = await self._playwright.chromium.launch(
            headless=self.headless,
            args=_LAUNCH_ARGS,
        )
        self._context = await self._browser.new_context(**_CONTEXT_KWARGS)
        await self._context.add_init_script(_INIT_SCRIPT)
        return self

    async def __aexit__(self, *_: object) -> None:
        if self._context:
            await self._context.close()
        if self._browser:
            await self._browser.close()
        if self._login_browser:
            await self._login_browser.close()
            self._login_browser = None
        if self._playwright:
            await self._playwright.stop()

    async def new_page(self) -> Page:
        if self._context is None:
            raise RuntimeError("Browser not launched. Use 'async with StealthBrowser()' first.")
        page = await self._context.new_page()
        await _stealth.apply_stealth_async(page)

        # Auto-accept JS dialogs (alert, confirm, prompt, beforeunload).
        # Headless pipelines cannot interact with native browser dialogs; without
        # this handler Playwright times out waiting for them to be dismissed.
        async def _auto_accept(dialog: Dialog) -> None:
            try:
                await dialog.accept()
            except Exception:
                pass

        page.on("dialog", _auto_accept)
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
        # human can actually interact with it.  Stored on self so __aexit__ closes it.
        self._login_browser = await self._playwright.chromium.launch(
            headless=False,
            args=_LAUNCH_ARGS,
        )
        try:
            login_context = await self._login_browser.new_context(**_CONTEXT_KWARGS)
            await login_context.add_init_script(_INIT_SCRIPT)
            page = await login_context.new_page()
            await _stealth.apply_stealth_async(page)
            await page.goto(url)

            print(
                f"[login] Navigate to {url} in the browser window, "
                "complete login, then press Enter here...",
                file=sys.stderr,
                flush=True,
            )

            loop = asyncio.get_running_loop()
            await loop.run_in_executor(None, input)

            # Trigger a cookie snapshot so callers can inspect session state
            # immediately after the handshake if needed. The actual cookies live on
            # the context and are accessible via page.context.cookies().
            await page.context.cookies()

            return page
        except Exception:
            await self._login_browser.close()
            self._login_browser = None
            raise
