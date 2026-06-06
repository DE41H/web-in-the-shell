import asyncio
import time
from unittest.mock import AsyncMock, patch

import httpx
import pytest
import respx

from network.dispatch.client import DispatchClient


# ---- basic GET/POST/PUT ----

@respx.mock
async def test_get_request(sample_session):
    respx.get("https://api.example.com/things").mock(
        return_value=httpx.Response(200, json={"ok": True})
    )
    async with DispatchClient(sample_session, base_url="https://api.example.com") as dc:
        response = await dc.get("/things")
    assert response.status_code == 200


@respx.mock
async def test_post_request(sample_session):
    route = respx.post("https://api.example.com/posts").mock(
        return_value=httpx.Response(201, json={"id": 2})
    )
    async with DispatchClient(sample_session, base_url="https://api.example.com") as dc:
        await dc.post("/posts", {"title": "x"})

    sent = route.calls.last.request
    assert sent.headers["content-type"] == "application/json"
    assert b'"title": "x"' in sent.content or b'"title":"x"' in sent.content


@respx.mock
async def test_put_request(sample_session):
    route = respx.put("https://api.example.com/posts/1").mock(
        return_value=httpx.Response(200, json={"id": 1})
    )
    async with DispatchClient(sample_session, base_url="https://api.example.com") as dc:
        await dc.put("/posts/1", {"title": "y"})

    sent = route.calls.last.request
    assert b'"title"' in sent.content


# ---- header / cookie injection ----

@respx.mock
async def test_post_includes_authorization_header(sample_session):
    route = respx.post("https://api.example.com/posts").mock(
        return_value=httpx.Response(200)
    )
    async with DispatchClient(sample_session, base_url="https://api.example.com") as dc:
        await dc.post("/posts", {})

    sent = route.calls.last.request
    assert sent.headers["Authorization"] == "Bearer test-token-xyz"


@respx.mock
async def test_post_includes_csrf_header(sample_session):
    route = respx.post("https://api.example.com/posts").mock(
        return_value=httpx.Response(200)
    )
    async with DispatchClient(sample_session, base_url="https://api.example.com") as dc:
        await dc.post("/posts", {})

    sent = route.calls.last.request
    csrf = sent.headers.get("X-CSRF-Token") or sent.headers.get("x-csrf-token")
    assert csrf == "csrf-abc"


@respx.mock
async def test_post_includes_cookies(sample_session):
    route = respx.post("https://api.example.com/posts").mock(
        return_value=httpx.Response(200)
    )
    async with DispatchClient(sample_session, base_url="https://api.example.com") as dc:
        await dc.post("/posts", {})

    sent = route.calls.last.request
    cookie_header = sent.headers.get("cookie", "") or sent.headers.get("Cookie", "")
    assert "session=abc123" in cookie_header


# ---- live header refresh on token rotation ----

@respx.mock
async def test_live_headers_refresh_on_token_rotation(sample_session):
    sample_session.credentials.bearer_token = "old"
    route_old = respx.post("https://api.example.com/old").mock(
        return_value=httpx.Response(200)
    )
    route_new = respx.post("https://api.example.com/new").mock(
        return_value=httpx.Response(200)
    )

    async with DispatchClient(sample_session, base_url="https://api.example.com") as dc:
        await dc.post("/old", {})
        sample_session.credentials.bearer_token = "new"
        await dc.post("/new", {})

    assert route_old.calls.last.request.headers["Authorization"] == "Bearer old"
    assert route_new.calls.last.request.headers["Authorization"] == "Bearer new"


# ---- semaphore ----

@respx.mock
async def test_semaphore_limits_concurrent(sample_session):
    counter = 0
    max_seen = 0

    async def slow_handler(request):
        nonlocal counter, max_seen
        counter += 1
        max_seen = max(max_seen, counter)
        await asyncio.sleep(0.05)
        counter -= 1
        return httpx.Response(200)

    respx.post("https://api.example.com/x").mock(side_effect=slow_handler)
    async with DispatchClient(
        sample_session, base_url="https://api.example.com", max_concurrent=2
    ) as dc:
        await asyncio.gather(*[dc.post("/x", {}) for _ in range(6)])
    assert max_seen == 2


# ---- response shape ----

@respx.mock
async def test_get_returns_response_with_status_code(sample_session):
    respx.get("https://api.example.com/things").mock(
        return_value=httpx.Response(204)
    )
    async with DispatchClient(sample_session, base_url="https://api.example.com") as dc:
        response = await dc.get("/things")
    assert response.status_code == 204


@respx.mock
async def test_post_returns_response_with_json(sample_session):
    respx.post("https://api.example.com/posts").mock(
        return_value=httpx.Response(201, json={"id": 7, "title": "ok"})
    )
    async with DispatchClient(sample_session, base_url="https://api.example.com") as dc:
        response = await dc.post("/posts", {})
    payload = response.json()
    assert payload["id"] == 7
    assert payload["title"] == "ok"


# ---- guard / lifecycle ----

async def test_method_called_without_context_manager_raises_assertion(sample_session):
    dc = DispatchClient(sample_session)
    with pytest.raises(AssertionError):
        await dc.post("/x", {})


@respx.mock
async def test_aexit_closes_httpx_client(sample_session):
    respx.get("https://api.example.com/things").mock(
        return_value=httpx.Response(200)
    )
    async with DispatchClient(sample_session, base_url="https://api.example.com") as dc:
        assert dc._client is not None
    assert dc._client is None


# ---- _guard full URL validation ----

@respx.mock
async def test_full_url_endpoint_is_validated(sample_session):
    respx.get("https://api.example.com/things").mock(
        return_value=httpx.Response(200)
    )
    async with DispatchClient(sample_session, base_url="https://api.example.com") as dc:
        with pytest.raises(ValueError):
            await dc.get("http://localhost/x")


@respx.mock
async def test_relative_endpoint_skips_url_validation(sample_session):
    respx.get("https://api.example.com/things").mock(
        return_value=httpx.Response(200)
    )
    async with DispatchClient(sample_session, base_url="https://api.example.com") as dc:
        response = await dc.get("/things")
    assert response.status_code == 200


@respx.mock
async def test_full_url_public_endpoint_allowed(sample_session):
    route = respx.get("https://other.example.org/items").mock(
        return_value=httpx.Response(200)
    )
    async with DispatchClient(sample_session, base_url="https://api.example.com") as dc:
        response = await dc.get("https://other.example.org/items")
    assert response.status_code == 200
    assert route.called


@patch("network.dispatch.client.httpx.AsyncClient")
async def test_follow_redirects_enabled(mock_async_client, sample_session):
    mock_client = MagicMock_async_ctx()
    mock_async_client.return_value = mock_client

    async with DispatchClient(sample_session, base_url="https://api.example.com"):
        pass

    assert mock_async_client.call_args.kwargs["follow_redirects"] is True


def MagicMock_async_ctx():
    from unittest.mock import MagicMock

    m = MagicMock()
    m.__aenter__ = AsyncMock(return_value=m)
    m.__aexit__ = AsyncMock(return_value=None)
    m.aclose = AsyncMock()
    return m


# ---- 429 retry / backoff ----

@respx.mock
async def test_dispatch_429_with_retry_after_header_is_retried(sample_session):
    route = respx.post("https://api.example.com/posts").mock(
        side_effect=[
            httpx.Response(429, headers={"retry-after": "0.1"}),
            httpx.Response(200, json={"id": 1}),
        ]
    )
    async with DispatchClient(sample_session, base_url="https://api.example.com") as dc:
        start = time.monotonic()
        response = await dc.post("/posts", {})
        elapsed = time.monotonic() - start
    assert response.status_code == 200
    assert route.call_count == 2
    assert elapsed >= 0.08


@respx.mock
async def test_dispatch_429_with_no_retry_after_uses_exponential_backoff(sample_session):
    route = respx.post("https://api.example.com/posts").mock(
        side_effect=[
            httpx.Response(429),
            httpx.Response(200),
        ]
    )
    async with DispatchClient(sample_session, base_url="https://api.example.com") as dc:
        start = time.monotonic()
        response = await dc.post("/posts", {})
        elapsed = time.monotonic() - start
    assert response.status_code == 200
    assert route.call_count == 2
    assert elapsed >= 0.9


@respx.mock
async def test_dispatch_429_returned_after_max_retries(sample_session):
    route = respx.post("https://api.example.com/posts").mock(
        side_effect=[
            httpx.Response(429),
            httpx.Response(429),
        ]
    )
    async with DispatchClient(
        sample_session, base_url="https://api.example.com", max_retries=1
    ) as dc:
        start = time.monotonic()
        response = await dc.post("/posts", {})
        elapsed = time.monotonic() - start
    assert response.status_code == 429
    assert route.call_count == 2
    assert elapsed >= 0.9


@respx.mock
async def test_dispatch_per_host_buckets_are_isolated(sample_session):
    respx.post("https://a.example.com/x").mock(return_value=httpx.Response(200))
    respx.post("https://b.example.com/x").mock(return_value=httpx.Response(200))
    async with DispatchClient(
        sample_session, requests_per_second=1, burst=1
    ) as dc:
        await dc.post("https://a.example.com/x", {})
        start = time.monotonic()
        await dc.post("https://b.example.com/x", {})
        elapsed = time.monotonic() - start
    assert elapsed < 0.3


@respx.mock
async def test_dispatch_rate_limiter_enforces_rps(sample_session):
    respx.get("https://api.example.com/x").mock(return_value=httpx.Response(200))
    async with DispatchClient(
        sample_session,
        base_url="https://api.example.com",
        requests_per_second=2,
        burst=2,
    ) as dc:
        start = time.monotonic()
        await asyncio.gather(*[dc.get("/x") for _ in range(3)])
        elapsed = time.monotonic() - start
    assert elapsed >= 0.4


@respx.mock
async def test_dispatch_does_not_retry_on_500(sample_session):
    route = respx.get("https://api.example.com/x").mock(
        return_value=httpx.Response(500)
    )
    async with DispatchClient(sample_session, base_url="https://api.example.com") as dc:
        response = await dc.get("/x")
    assert response.status_code == 500
    assert route.call_count == 1


# ── patch() ──────────────────────────────────────────────────────────────────

@respx.mock
async def test_patch_sends_json_body_and_live_headers(sample_session):
    route = respx.patch("https://api.example.com/posts/1").mock(
        return_value=httpx.Response(200, json={"id": 1})
    )
    async with DispatchClient(sample_session, base_url="https://api.example.com") as dc:
        await dc.patch("/posts/1", {"title": "updated"})

    sent = route.calls.last.request
    assert b'"title"' in sent.content
    assert sent.headers["Authorization"] == "Bearer test-token-xyz"


@respx.mock
async def test_patch_with_absolute_url_validates_host(sample_session):
    async with DispatchClient(sample_session, base_url="https://api.example.com") as dc:
        with pytest.raises(ValueError):
            await dc.patch("http://localhost/x", {"data": "val"})


@respx.mock
async def test_patch_acquires_and_releases_semaphore(sample_session):
    counter = 0
    max_seen = 0

    async def slow_handler(request):
        nonlocal counter, max_seen
        counter += 1
        max_seen = max(max_seen, counter)
        await asyncio.sleep(0.05)
        counter -= 1
        return httpx.Response(200)

    respx.patch("https://api.example.com/x").mock(side_effect=slow_handler)
    async with DispatchClient(
        sample_session, base_url="https://api.example.com", max_concurrent=2
    ) as dc:
        await asyncio.gather(*[dc.patch("/x", {}) for _ in range(6)])
    assert max_seen == 2


# ── delete() ─────────────────────────────────────────────────────────────────

@respx.mock
async def test_delete_request(sample_session):
    """C3 — DispatchClient.delete() fires a DELETE HTTP method."""
    route = respx.delete("https://api.example.com/posts/1").mock(
        return_value=httpx.Response(204)
    )
    async with DispatchClient(sample_session, base_url="https://api.example.com") as dc:
        response = await dc.delete("/posts/1")
    assert response.status_code == 204
    assert route.called


@respx.mock
async def test_delete_includes_live_headers(sample_session):
    """C3 — delete() injects Authorization header from live credentials."""
    route = respx.delete("https://api.example.com/posts/1").mock(
        return_value=httpx.Response(204)
    )
    async with DispatchClient(sample_session, base_url="https://api.example.com") as dc:
        await dc.delete("/posts/1")
    sent = route.calls.last.request
    assert sent.headers["Authorization"] == "Bearer test-token-xyz"


@respx.mock
async def test_delete_with_absolute_url_validates_host(sample_session):
    """C3 — delete() rejects SSRF-blocked hosts."""
    async with DispatchClient(sample_session, base_url="https://api.example.com") as dc:
        with pytest.raises(ValueError):
            await dc.delete("http://localhost/x")


# ── Cookie header is live (C2) ────────────────────────────────────────────────

@respx.mock
async def test_cookie_header_set_from_live_credentials(sample_session):
    """C2 — Cookie header is assembled from live credentials at call time."""
    route = respx.post("https://api.example.com/posts").mock(
        return_value=httpx.Response(200)
    )
    async with DispatchClient(sample_session, base_url="https://api.example.com") as dc:
        await dc.post("/posts", {})

    sent = route.calls.last.request
    # Must be in Cookie: header, NOT baked into the httpx client cookie jar
    cookie_header = sent.headers.get("cookie", "") or sent.headers.get("Cookie", "")
    assert "session=abc123" in cookie_header


@respx.mock
async def test_cookie_header_reflects_updated_credentials(sample_session):
    """C2 — Cookie header reflects credential changes made after client open."""
    route = respx.post("https://api.example.com/posts").mock(
        return_value=httpx.Response(200)
    )
    async with DispatchClient(sample_session, base_url="https://api.example.com") as dc:
        # Update cookies after client is already open
        sample_session.credentials.cookies = {"session": "new-token-xyz"}
        await dc.post("/posts", {})

    sent = route.calls.last.request
    cookie_header = sent.headers.get("cookie", "") or sent.headers.get("Cookie", "")
    assert "new-token-xyz" in cookie_header
    assert "abc123" not in cookie_header


@respx.mock
async def test_no_cookie_header_when_credentials_empty(sample_session):
    """C2 — No Cookie header when credentials.cookies is empty."""
    sample_session.credentials.cookies = {}
    route = respx.post("https://api.example.com/posts").mock(
        return_value=httpx.Response(200)
    )
    async with DispatchClient(sample_session, base_url="https://api.example.com") as dc:
        await dc.post("/posts", {})

    sent = route.calls.last.request
    assert "cookie" not in {k.lower() for k in sent.headers.keys()}


# ── max_retries=2 default (M7) ────────────────────────────────────────────────

@respx.mock
async def test_default_max_retries_is_2(sample_session):
    """M7 — Default max_retries is 2: 3 total calls (1 + 2 retries)."""
    route = respx.post("https://api.example.com/posts").mock(
        side_effect=[
            httpx.Response(429, headers={"retry-after": "0.01"}),
            httpx.Response(429, headers={"retry-after": "0.01"}),
            httpx.Response(200),
        ]
    )
    async with DispatchClient(sample_session, base_url="https://api.example.com") as dc:
        response = await dc.post("/posts", {})
    assert response.status_code == 200
    assert route.call_count == 3


# ── Retry-After: 0 triggers immediate retry (M7) ─────────────────────────────

@respx.mock
async def test_retry_after_zero_triggers_immediate_retry(sample_session):
    """M7 — Retry-After: 0 means retry immediately, not exponential backoff."""
    route = respx.post("https://api.example.com/posts").mock(
        side_effect=[
            httpx.Response(429, headers={"retry-after": "0"}),
            httpx.Response(200),
        ]
    )
    async with DispatchClient(
        sample_session, base_url="https://api.example.com", max_retries=1
    ) as dc:
        start = time.monotonic()
        response = await dc.post("/posts", {})
        elapsed = time.monotonic() - start
    assert response.status_code == 200
    assert route.call_count == 2
    # Must be fast — no 1-second exponential backoff
    assert elapsed < 0.5
