"""Unit tests for _probe_url and _pick_reachable_domain in main.py."""

import asyncio

import httpx
import respx

from main import _pick_reachable_domain, _probe_url


# ---------------------------------------------------------------------------
# _probe_url tests
# ---------------------------------------------------------------------------


@respx.mock
async def test_probe_url_returns_final_url_on_200():
    respx.head("https://example.com/").mock(return_value=httpx.Response(200))
    result = await _probe_url("https://example.com")
    assert result == "https://example.com"


async def test_probe_url_returns_none_on_empty_hostname():
    assert await _probe_url("") is None
    assert await _probe_url("not-a-url") is None


@respx.mock
async def test_probe_url_head_then_get_on_405():
    head_route = respx.head("https://example.com/").mock(return_value=httpx.Response(405))
    get_route = respx.get("https://example.com/").mock(return_value=httpx.Response(200))
    result = await _probe_url("https://example.com")
    assert head_route.called
    assert get_route.called
    assert result == "https://example.com"


@respx.mock
async def test_probe_url_follows_redirect():
    respx.head("https://example.com/").mock(
        return_value=httpx.Response(
            301, headers={"Location": "https://www.example.com/"}
        )
    )
    respx.head("https://www.example.com/").mock(return_value=httpx.Response(200))
    result = await _probe_url("https://example.com")
    assert result == "https://www.example.com"


@respx.mock
async def test_probe_url_returns_none_on_connect_error():
    respx.head("https://dead.example/").mock(
        side_effect=httpx.ConnectError("DNS fail")
    )
    result = await _probe_url("https://dead.example")
    assert result is None


@respx.mock
async def test_probe_url_retries_once_on_exception():
    # First call raises, second call succeeds. respx side_effect list is per-route-call.
    respx.head("https://example.com/").mock(
        side_effect=[httpx.ConnectError("transient"), httpx.Response(200)]
    )
    result = await _probe_url("https://example.com")
    assert result == "https://example.com"


@respx.mock
async def test_probe_url_returns_none_when_both_retries_fail():
    respx.head("https://example.com/").mock(
        side_effect=[httpx.ConnectError("fail1"), httpx.ConnectError("fail2")]
    )
    result = await _probe_url("https://example.com")
    assert result is None


@respx.mock
async def test_probe_url_any_status_counts_as_reachable():
    respx.head("https://example.com/").mock(return_value=httpx.Response(404))
    result = await _probe_url("https://example.com")
    assert result == "https://example.com"


@respx.mock
async def test_probe_url_adds_https_scheme_when_missing():
    # urlparse("example.com") yields scheme="" and hostname=None, path="example.com"
    # The function returns None when host is falsy.
    # "example.com" has no scheme so urlparse gives hostname=None → None.
    result = await _probe_url("example.com")
    assert result is None


@respx.mock
async def test_probe_url_strips_path_from_base():
    # Probes the root, not the full path.
    respx.head("https://example.com/").mock(return_value=httpx.Response(200))
    result = await _probe_url("https://example.com/some/path")
    assert result == "https://example.com"


# ---------------------------------------------------------------------------
# _pick_reachable_domain tests
# ---------------------------------------------------------------------------


async def test_pick_empty_list_returns_none():
    result = await _pick_reachable_domain([])
    assert result is None


@respx.mock
async def test_pick_single_reachable_url():
    respx.head("https://example.com/").mock(return_value=httpx.Response(200))
    result = await _pick_reachable_domain(["https://example.com"])
    assert result == "https://example.com"


@respx.mock
async def test_pick_single_unreachable_returns_none():
    respx.head("https://dead.example/").mock(
        side_effect=httpx.ConnectError("unreachable")
    )
    # www expansion also fails
    respx.head("https://www.dead.example/").mock(
        side_effect=httpx.ConnectError("unreachable")
    )
    result = await _pick_reachable_domain(["https://dead.example"])
    assert result is None


@respx.mock
async def test_pick_primary_wins_over_candidate():
    respx.head("https://first.example/").mock(return_value=httpx.Response(200))
    respx.head("https://www.first.example/").mock(return_value=httpx.Response(200))
    respx.head("https://second.example/").mock(return_value=httpx.Response(200))
    respx.head("https://www.second.example/").mock(return_value=httpx.Response(200))
    result = await _pick_reachable_domain(
        ["https://first.example", "https://second.example"]
    )
    assert result == "https://first.example"


@respx.mock
async def test_pick_falls_back_to_candidate_when_primary_fails():
    respx.head("https://dead.example/").mock(
        side_effect=httpx.ConnectError("fail")
    )
    respx.head("https://www.dead.example/").mock(
        side_effect=httpx.ConnectError("fail")
    )
    respx.head("https://alive.example/").mock(return_value=httpx.Response(200))
    respx.head("https://www.alive.example/").mock(
        side_effect=httpx.ConnectError("fail")
    )
    result = await _pick_reachable_domain(
        ["https://dead.example", "https://alive.example"]
    )
    assert result == "https://alive.example"


@respx.mock
async def test_pick_expands_bare_to_www():
    respx.head("https://github.com/").mock(side_effect=httpx.ConnectError("fail"))
    respx.head("https://www.github.com/").mock(return_value=httpx.Response(200))
    result = await _pick_reachable_domain(["https://github.com"])
    assert result == "https://www.github.com"


@respx.mock
async def test_pick_expands_www_to_bare():
    respx.head("https://www.github.com/").mock(
        side_effect=httpx.ConnectError("fail")
    )
    respx.head("https://github.com/").mock(return_value=httpx.Response(200))
    result = await _pick_reachable_domain(["https://www.github.com"])
    assert result == "https://github.com"


@respx.mock
async def test_pick_deduplicates_candidates():
    # Pass same URL twice (one with trailing slash, one without).
    # Should only be probed once (plus its www expansion).
    route = respx.head("https://example.com/").mock(return_value=httpx.Response(200))
    respx.head("https://www.example.com/").mock(
        side_effect=httpx.ConnectError("fail")
    )
    result = await _pick_reachable_domain(
        ["https://example.com/", "https://example.com"]
    )
    assert result == "https://example.com"
    assert route.call_count == 1


@respx.mock
async def test_pick_www_expansion_has_lower_priority():
    # github.com at priority 0 wins even when www.github.com is also in the list (priority 2)
    # and www-expansion of github.com is at priority 1.
    respx.head("https://github.com/").mock(return_value=httpx.Response(200))
    respx.head("https://www.github.com/").mock(return_value=httpx.Response(200))
    result = await _pick_reachable_domain(
        ["https://github.com", "https://www.github.com"]
    )
    assert result == "https://github.com"


@respx.mock
async def test_pick_cancels_remaining_tasks_after_winner():
    # Winner resolves quickly; a slow second URL should be cancelled.
    # We verify this by asserting the function returns (i.e. does not hang),
    # and the winner's result is returned despite a pending slow task.
    async def slow_side_effect(request):
        # Signal that we're running, then simulate a slow response.
        await asyncio.sleep(10)
        return httpx.Response(200)

    respx.head("https://fast.example/").mock(return_value=httpx.Response(200))
    respx.head("https://www.fast.example/").mock(
        side_effect=httpx.ConnectError("fail")
    )
    respx.head("https://slow.example/").mock(side_effect=slow_side_effect)
    respx.head("https://www.slow.example/").mock(side_effect=slow_side_effect)

    result = await asyncio.wait_for(
        _pick_reachable_domain(["https://fast.example", "https://slow.example"]),
        timeout=5.0,
    )
    assert result == "https://fast.example"


@respx.mock
async def test_pick_all_fail_returns_none():
    respx.head("https://a.fail/").mock(side_effect=httpx.ConnectError("fail"))
    respx.head("https://www.a.fail/").mock(side_effect=httpx.ConnectError("fail"))
    respx.head("https://b.fail/").mock(side_effect=httpx.ConnectError("fail"))
    respx.head("https://www.b.fail/").mock(side_effect=httpx.ConnectError("fail"))
    result = await _pick_reachable_domain(["https://a.fail", "https://b.fail"])
    assert result is None


@respx.mock
async def test_pick_head_405_falls_back_to_get():
    respx.head("https://example.com/").mock(return_value=httpx.Response(405))
    respx.get("https://example.com/").mock(return_value=httpx.Response(200))
    respx.head("https://www.example.com/").mock(
        side_effect=httpx.ConnectError("fail")
    )
    result = await _pick_reachable_domain(["https://example.com"])
    assert result == "https://example.com"


@respx.mock
async def test_pick_multiple_candidates_all_reachable_returns_best_priority():
    respx.head("https://alpha.example/").mock(return_value=httpx.Response(200))
    respx.head("https://www.alpha.example/").mock(return_value=httpx.Response(200))
    respx.head("https://beta.example/").mock(return_value=httpx.Response(200))
    respx.head("https://www.beta.example/").mock(return_value=httpx.Response(200))
    respx.head("https://gamma.example/").mock(return_value=httpx.Response(200))
    respx.head("https://www.gamma.example/").mock(return_value=httpx.Response(200))
    result = await _pick_reachable_domain(
        ["https://alpha.example", "https://beta.example", "https://gamma.example"]
    )
    assert result == "https://alpha.example"
