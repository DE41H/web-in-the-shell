import asyncio
import urllib.parse

import httpx

from network.dispatch.ratelimit import TokenBucket, parse_retry_after
from network.session.manager import SessionManager
from security.allowlist import validate_url


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
        self._session = session
        self._base_url = base_url
        self._client: httpx.AsyncClient | None = None
        self._sem = asyncio.Semaphore(max_concurrent)
        self._rps = requests_per_second
        self._burst = burst
        self._max_retries = max_retries
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

    async def _do(self, method: str, url: str, **kwargs) -> httpx.Response:
        host = self._host_for(url)
        bucket = await self._get_bucket(host)
        attempt = 0
        while True:
            await bucket.acquire()
            response = await self._client.request(
                method, url, headers=self._live_headers(), **kwargs
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

    async def get(self, endpoint: str, **kwargs) -> httpx.Response:
        self._guard(endpoint)
        async with self._sem:
            return await self._do("GET", endpoint, **kwargs)

    async def post(self, endpoint: str, payload: dict, **kwargs) -> httpx.Response:
        self._guard(endpoint)
        async with self._sem:
            return await self._do("POST", endpoint, json=payload, **kwargs)

    async def put(self, endpoint: str, payload: dict, **kwargs) -> httpx.Response:
        self._guard(endpoint)
        async with self._sem:
            return await self._do("PUT", endpoint, json=payload, **kwargs)

    async def patch(self, endpoint: str, payload: dict, **kwargs) -> httpx.Response:
        self._guard(endpoint)
        async with self._sem:
            return await self._do("PATCH", endpoint, json=payload, **kwargs)

    async def delete(self, url: str, **kwargs) -> httpx.Response:
        self._guard(url)
        async with self._sem:
            return await self._do("DELETE", url, **kwargs)
