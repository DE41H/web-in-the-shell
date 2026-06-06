from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import TYPE_CHECKING

from playwright.async_api import Page, Request

if TYPE_CHECKING:
    from persistence.session_store import SessionStore


_BEARER_RE = re.compile(r"Bearer\s+([A-Za-z0-9\-._~+/]+=*)", re.IGNORECASE)
_CSRF_HEADERS = {"x-csrf-token", "x-xsrf-token", "csrf-token", "x-request-token"}
_MIRROR_HEADERS = {"x-requested-with", "x-api-key", "x-client-id", "x-app-version"}


@dataclass
class SessionCredentials:
    cookies: dict[str, str] = field(default_factory=dict)
    bearer_token: str | None = None
    csrf_token: str | None = None
    extra_headers: dict[str, str] = field(default_factory=dict)

    def as_headers(self) -> dict[str, str]:
        headers = dict(self.extra_headers)
        if self.bearer_token:
            headers["Authorization"] = f"Bearer {self.bearer_token}"
        if self.csrf_token:
            headers["X-CSRF-Token"] = self.csrf_token
        return headers


class SessionManager:
    """
    Watches outgoing requests and extracts auth material (tokens, CSRF, cookies)
    as they appear on the wire. Credentials stay live — if the app rotates a
    token mid-session, the next as_headers() call reflects it automatically.

    Optional persistence
    --------------------
    When a ``SessionStore`` is passed to :meth:`persist` or :meth:`restore`,
    credentials are mirrored to (or primed from) the local sqlite store. The
    live browser session is always the source of truth at runtime; the store
    is a rehydration hint for the next run.
    """

    def __init__(self) -> None:
        self.credentials = SessionCredentials()

    async def _on_request(self, request: Request) -> None:
        headers = request.headers

        match = _BEARER_RE.search(headers.get("authorization", ""))
        if match:
            self.credentials.bearer_token = match.group(1)

        for name, value in headers.items():
            if name.lower() in _CSRF_HEADERS:
                self.credentials.csrf_token = value
                break

        # Playwright normalises all request-header keys to lowercase before
        # handing them to this callback, so a plain `key in headers` lookup is
        # safe here — _MIRROR_HEADERS already contains only lowercase strings.
        for key in _MIRROR_HEADERS:
            if key in headers:
                self.credentials.extra_headers[key] = headers[key]

    def attach(self, page: Page) -> None:
        page.on("request", self._on_request)

    async def sync_cookies(self, page: Page) -> None:
        raw = await page.context.cookies()
        self.credentials.cookies = {c["name"]: c["value"] for c in raw}

    async def restore(self, host: str, store: SessionStore) -> bool:
        """Prime ``self.credentials`` from *store* for *host*. Returns ``True``
        if a row was loaded. Live values are preferred: stored values only
        fill fields that are currently empty.
        """
        creds = await store.get(host)
        if creds is None:
            return False
        if creds.cookies and not self.credentials.cookies:
            self.credentials.cookies = dict(creds.cookies)
        if creds.bearer_token and not self.credentials.bearer_token:
            self.credentials.bearer_token = creds.bearer_token
        if creds.csrf_token and not self.credentials.csrf_token:
            self.credentials.csrf_token = creds.csrf_token
        if creds.extra_headers:
            merged = dict(creds.extra_headers)      # start with stored
            merged.update(self.credentials.extra_headers)  # live overwrites stored
            self.credentials.extra_headers = merged
        return True

    @property
    def has_material(self) -> bool:
        """Return True if any credential field has material value."""
        c = self.credentials
        return bool(c.bearer_token or c.csrf_token or c.cookies or c.extra_headers)

    async def persist(self, host: str, store: SessionStore) -> None:
        """Mirror the current credentials to *store* for *host*.

        No-op when no credential field has material (avoids writing empty rows).
        """
        if not self.has_material:
            return
        await store.save(host, self.credentials)
