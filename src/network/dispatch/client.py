import asyncio
import urllib.parse
from typing import Any

import httpx
from pydantic import BaseModel, Field

from network.dispatch.headers import USER_AGENT, SEC_CH_UA, SEC_FETCH_HEADERS
from network.dispatch.ratelimit import TokenBucket, parse_retry_after
from network.dispatch.ssrf_transport import SSRFTransport
from network.session.manager import SessionManager
from security.allowlist import validate_url
from network.dispatch.request_builder import RequestSpec


class DispatchConfig(BaseModel):
    """Pydantic model for DispatchClient configuration.

    This centralises parameter validation and documents the client limits.
    """

    base_url: str = Field(default="", max_length=200)
    max_concurrent: int = Field(default=5, ge=1, le=100)
    requests_per_second: float = Field(default=5.0, ge=0.1)
    burst: int = Field(default=10, ge=1)
    max_retries: int = Field(default=2, ge=0, le=10)


class DispatchClient:
    """
    Async HTTP client that injects live session credentials on every request.
    Headers are re-read from SessionManager at call time so rotated tokens
    are applied without rebuilding the client.

    max_concurrent caps the number of in-flight HTTP requests so that a
    multi-step plan or retry loop cannot fan-out-flood the target API.

    requests_per_second + burst form a per-host token bucket: each request
    consumes one token; the bucket refills continuously at the given rate.
    max_retries retries 429 responses, honouring the Retry-After header
    when present and falling back to exponential backoff otherwise.
    """

    def __init__(
        self,
        session: SessionManager,
        base_url: str = "",
        max_concurrent: int = 5,
        requests_per_second: float = 5.0,
        burst: int = 10,
        max_retries: int = 2,
    ) -> None:
        # Validate and store configuration via a pydantic model
        cfg = DispatchConfig(
            base_url=base_url,
            max_concurrent=max_concurrent,
            requests_per_second=requests_per_second,
            burst=burst,
            max_retries=max_retries,
        )
        self._session = session
        self._base_url = cfg.base_url
        self._client: httpx.AsyncClient | None = None
        self._sem = asyncio.Semaphore(cfg.max_concurrent)
        self._rps = cfg.requests_per_second
        self._burst = cfg.burst
        self._max_retries = cfg.max_retries
        self._buckets: dict[str, TokenBucket] = {}
        self._bucket_lock = asyncio.Lock()

    async def __aenter__(self) -> "DispatchClient":
        if self._base_url:
            validate_url(self._base_url)  # SSRF guard — checked once at client open time
        self._client = httpx.AsyncClient(
            base_url=self._base_url,
            timeout=httpx.Timeout(30.0),
            follow_redirects=True,
            limits=httpx.Limits(max_connections=10, max_keepalive_connections=5),
            transport=SSRFTransport(),
        )
        return self

    async def __aexit__(self, *_: object) -> None:
        if self._client:
            await self._client.aclose()
            self._client = None

    def _live_headers(self) -> dict[str, str]:
        h = self._session.credentials.as_headers()
        creds = self._session.credentials
        if creds.cookies:
            h["Cookie"] = "; ".join(f"{k}={v}" for k, v in creds.cookies.items())
        # Inject realistic browser headers so sites don't 403/406 on missing UA,
        # client-hint mismatches, or missing Accept. All use setdefault so that
        # session credentials (bearer, CSRF, extra_headers) always win.
        h.setdefault("User-Agent", USER_AGENT)
        h.setdefault("Accept", "application/json, text/html, */*;q=0.8")
        h.setdefault("Accept-Language", "en-US,en;q=0.9")
        h.setdefault("Accept-Encoding", "gzip, deflate, br")
        h.setdefault("sec-ch-ua", SEC_CH_UA)
        h.setdefault("sec-ch-ua-mobile", "?0")
        h.setdefault("sec-ch-ua-platform", '"Windows"')
        return h

    def _guard(self, endpoint: str) -> None:
        assert self._client, "Use 'async with DispatchClient()'"
        if endpoint.startswith("http"):
            validate_url(endpoint)  # full URL passed directly — validate the host

    @property
    def _base_url_host(self) -> str:
        return urllib.parse.urlparse(self._base_url).hostname or ""

    def _host_for(self, url: str) -> str:
        return urllib.parse.urlparse(url).hostname or self._base_url_host

    async def _get_bucket(self, host: str) -> TokenBucket:
        async with self._bucket_lock:
            bucket = self._buckets.get(host)
            if bucket is None:
                bucket = TokenBucket(self._burst, self._rps)
                self._buckets[host] = bucket
            return bucket

    async def _do(self, method: str, url: str, **kwargs: Any) -> httpx.Response:
        assert self._client is not None
        host = self._host_for(url)
        bucket = await self._get_bucket(host)
        # Snapshot caller-supplied headers and content before the retry loop so
        # that each attempt gets the same values (pop inside the loop would
        # silently drop them after the first attempt).
        req_headers = kwargs.pop("headers", None) or {}
        content = kwargs.pop("content", None)
        attempt = 0
        parsed_url = urllib.parse.urlparse(url)
        origin = f"{parsed_url.scheme}://{parsed_url.netloc}"
        while True:
            await bucket.acquire()
            merged_headers = dict(self._live_headers())
            merged_headers.update(req_headers)

            # Origin and Referer make the request look like normal in-site
            # navigation. Both use setdefault so callers can override.
            merged_headers.setdefault("Referer", origin + "/")
            if method.upper() not in ("GET", "HEAD"):
                merged_headers.setdefault("Origin", origin)

            # Sec-Fetch-* headers are sent by Chrome on every request; WAFs flag
            # their absence as a bot signal.
            for k, v in SEC_FETCH_HEADERS.items():
                merged_headers.setdefault(k, v)

            if content is not None and not isinstance(content, (str, bytes)) and (
                hasattr(content, "__aiter__") or hasattr(content, "__iter__")
            ):
                # httpx accepts an (async) iterator as 'content' for streaming
                response = await self._client.request(
                    method, url, headers=merged_headers, content=content, **kwargs
                )
            else:
                response = await self._client.request(
                    method, url, headers=merged_headers, **kwargs
                )
            if response.status_code != 429 or attempt >= self._max_retries:
                return response
            retry_after_hdr = response.headers.get("retry-after")
            if retry_after_hdr is not None:
                # Server provided a Retry-After header — honor it exactly.
                # parse_retry_after("0") returns 0.0, meaning retry immediately.
                delay: float = parse_retry_after(retry_after_hdr)
            else:
                # No Retry-After header — fall back to exponential backoff.
                delay = float(2 ** attempt)
            await asyncio.sleep(delay)
            attempt += 1

    async def get(self, endpoint: str, **kwargs: Any) -> httpx.Response:
        self._guard(endpoint)
        async with self._sem:
            return await self._do("GET", endpoint, **kwargs)

    async def post(self, endpoint: str, payload: dict[str, Any], **kwargs: Any) -> httpx.Response:
        self._guard(endpoint)
        async with self._sem:
            return await self._do("POST", endpoint, json=payload, **kwargs)

    async def put(self, endpoint: str, payload: dict[str, Any], **kwargs: Any) -> httpx.Response:
        self._guard(endpoint)
        async with self._sem:
            return await self._do("PUT", endpoint, json=payload, **kwargs)

    async def patch(self, endpoint: str, payload: dict[str, Any], **kwargs: Any) -> httpx.Response:
        self._guard(endpoint)
        async with self._sem:
            return await self._do("PATCH", endpoint, json=payload, **kwargs)

    async def delete(self, url: str, **kwargs: Any) -> httpx.Response:
        self._guard(url)
        async with self._sem:
            return await self._do("DELETE", url, **kwargs)

    async def send_spec(self, spec: RequestSpec) -> httpx.Response:
        """Send a RequestSpec using the live session headers/cookies.

        This helper centralises header+cookie merging and allows callers to
        construct requests declaratively.
        """
        if spec.url:
            self._guard(spec.url)
        kwargs = spec.to_httpx_kwargs(
            session_headers=self._live_headers(),
            session_cookies=self._session.credentials.cookies,
        )
        # Resolve relative URLs against base_url if necessary
        target = spec.url
        if not target:
            target = self._base_url
        elif target.startswith("/") and self._base_url:
            # join path with base_url
            from urllib.parse import urljoin

            target = urljoin(self._base_url, target)

        async with self._sem:
            # Support streaming content by passing through kwargs['content'] as-is
            return await self._do(spec.method.upper(), target, **kwargs)
