"""Autonomous browser interaction helpers.

Provides three capabilities that run without user involvement:
- dismiss_consent: click GDPR/cookie-consent banners using known CMP selectors
  and a JS text-matching fallback.
- dismiss_overlays: close newsletter pop-ups and generic modal overlays.
- detect_captcha: identify the CAPTCHA type blocking the page, if any.
"""
import asyncio
from typing import Any

# ── Consent management platforms (CMPs) ────────────────────────────────────
# Ordered by market-share so we hit the most common ones first.
_CONSENT_SELECTORS: list[str] = [
    "#onetrust-accept-btn-handler",                              # OneTrust
    "#CybotCookiebotDialogBodyLevelButtonLevelOptinAllowAll",   # Cookiebot
    ".fc-cta-consent",                                          # Funding Choices (Google)
    ".qc-cmp2-summary-buttons button:first-child",              # Quantcast
    "#sp-cc-accept",                                            # Sourcepoint
    "#didomi-notice-agree-button",                              # Didomi
    "#accept-recommended-btn-handler",                          # OneTrust variant
    "button#accept-cookies",
    "button#cookie-accept",
    "button#acceptBtn",
    "button#accept_all",
    "button#acceptAllButton",
    "button[data-testid='accept-all-cookies']",
    "button[data-testid='cookie-accept']",
    "button[data-testid='uc-accept-all-button']",
    ".cookie-consent__accept",
    ".js-accept-cookies",
    ".cc-accept",
    "[aria-label='Accept cookies']",
    "[aria-label='Accept all cookies']",
    "[aria-label='Agree to our data processing and close']",
]

# JS fallback: click the first visible button whose full text exactly matches
# one of the accept keywords (most-specific first to avoid false positives).
_CONSENT_JS = """
() => {
    const EXACT = [
        'accept all', 'accept cookies', 'allow all cookies', 'allow all',
        'i agree', 'agree and continue', 'got it', 'ok, got it', 'ok',
        'i understand', 'allow cookies', 'agree', 'accept',
    ];
    const PREFIX = ['accept all', 'allow all', 'i agree', 'agree to'];
    function matches(el) {
        const t = (el.textContent || '').trim().toLowerCase().replace(/\\s+/g, ' ');
        if (EXACT.includes(t)) return 3;
        if (PREFIX.some(p => t.startsWith(p))) return 2;
        return 0;
    }
    const visible = Array.from(document.querySelectorAll(
        'button, a[role="button"], [role="button"], input[type="button"]'
    )).filter(el => {
        const r = el.getBoundingClientRect();
        return r.width > 0 && r.height > 0 && r.top >= 0 && r.top < window.innerHeight;
    });
    visible.sort((a, b) => matches(b) - matches(a));
    const best = visible.find(el => matches(el) > 0);
    if (best) { best.click(); return true; }
    return false;
}
"""

# ── Generic modal overlays ──────────────────────────────────────────────────
_OVERLAY_SELECTORS: list[str] = [
    "button[aria-label='Close']",
    "button[aria-label='Dismiss']",
    "button[aria-label='close']",
    "[data-dismiss='modal']",
    ".modal-close",
    ".modal__close",
    ".close-button",
    ".popup-close",
    ".popup__close",
    "[data-testid='close-button']",
    "[data-testid='modal-close']",
    ".email-signup-modal__close",
    ".newsletter-modal__close",
]

_OVERLAY_JS = """
() => {
    const WORDS = [
        'no thanks', 'no, thanks', 'dismiss', 'close', 'skip',
        'maybe later', 'not now', 'no thank you',
    ];
    const visible = Array.from(document.querySelectorAll(
        'button, a[role="button"], [role="button"]'
    )).filter(el => {
        const r = el.getBoundingClientRect();
        return r.width > 0 && r.height > 0 && r.top >= 0;
    });
    const best = visible.find(el => {
        const t = (el.textContent || '').trim().toLowerCase().replace(/\\s+/g, ' ');
        return WORDS.some(w => t === w || t.includes(w));
    });
    if (best) { best.click(); return true; }
    return false;
}
"""

# ── CAPTCHA fingerprints ────────────────────────────────────────────────────
# Each entry: (display-name, CSS selector).
_CAPTCHA_CHECKS: list[tuple[str, str]] = [
    ("hCaptcha",             "iframe[src*='hcaptcha.com']"),
    ("reCAPTCHA",            "iframe[src*='recaptcha/api']"),
    ("reCAPTCHA",            "iframe[title*='reCAPTCHA']"),
    ("Cloudflare Turnstile", "iframe[src*='challenges.cloudflare.com']"),
    ("Cloudflare Turnstile", ".cf-turnstile"),
    ("Cloudflare Turnstile", "#cf-challenge-running"),
    ("Arkose Labs",          "iframe[src*='funcaptcha']"),
    ("Arkose Labs",          "iframe[src*='arkoselabs']"),
    ("DataDome",             "iframe[src*='geo.captcha-delivery.com']"),
    ("generic CAPTCHA",
     "[id*='captcha']:not([id*='nocaptcha']):not([id*='recaptcha-anchor'])"),
    ("generic CAPTCHA",      "[class*='captcha']:not([class*='nocaptcha'])"),
]


async def dismiss_consent(page: Any) -> bool:
    """Try to auto-dismiss a cookie/GDPR consent banner.

    Tries known CMP selectors first, then falls back to JS text matching.
    Returns True if something was clicked.
    """
    for sel in _CONSENT_SELECTORS:
        try:
            el = await page.query_selector(sel)
            if el and await el.is_visible():
                await el.click()
                await asyncio.sleep(0.5)
                return True
        except Exception:
            continue

    try:
        if await page.evaluate(_CONSENT_JS):
            await asyncio.sleep(0.5)
            return True
    except Exception:
        pass

    return False


async def dismiss_overlays(page: Any) -> bool:
    """Try to auto-close newsletter pop-ups and generic modal overlays.

    Returns True if something was clicked.
    """
    for sel in _OVERLAY_SELECTORS:
        try:
            el = await page.query_selector(sel)
            if el and await el.is_visible():
                await el.click()
                await asyncio.sleep(0.3)
                return True
        except Exception:
            continue

    try:
        if await page.evaluate(_OVERLAY_JS):
            await asyncio.sleep(0.3)
            return True
    except Exception:
        pass

    return False


async def detect_captcha(page: Any) -> str | None:
    """Return the CAPTCHA provider name if a CAPTCHA widget is visible, else None."""
    for name, sel in _CAPTCHA_CHECKS:
        try:
            el = await page.query_selector(sel)
            if el and await el.is_visible():
                return name
        except Exception:
            continue
    return None
