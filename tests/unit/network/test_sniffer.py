import json
from unittest.mock import AsyncMock, MagicMock

from network.intercept.sniffer import CapturedResponse, PacketSniffer


def _cap(url: str, body: dict | None = None) -> CapturedResponse:
    body_bytes = json.dumps(body).encode() if body is not None else b""
    return CapturedResponse(
        url=url,
        status=200,
        headers={"content-type": "application/json"},
        body=body_bytes,
        json=body,
    )


def _mock_response(
    url: str = "https://x.com/api",
    status: int = 200,
    headers: dict | None = None,
    body: bytes = b"",
    json_body=None,
    *,
    body_raises: bool = False,
    json_raises: bool = False,
) -> MagicMock:
    response = MagicMock()
    response.url = url
    response.status = status
    response.headers = headers if headers is not None else {}
    if body_raises:
        response.body = AsyncMock(side_effect=Exception("body unavailable"))
    else:
        response.body = AsyncMock(return_value=body)
    if json_raises:
        response.json = AsyncMock(side_effect=Exception("not json"))
    else:
        response.json = AsyncMock(return_value=json_body)
    return response


# ---- pattern matching ----

def test_matches_returns_true_for_matching_url():
    sniffer = PacketSniffer([r"/api/.*"])
    assert any(p.search("https://x.com/api/users") for p in sniffer._patterns) is True


def test_matches_returns_false_for_non_matching_url():
    sniffer = PacketSniffer([r"/api/.*"])
    assert any(p.search("https://x.com/static/img.png") for p in sniffer._patterns) is False


def test_multiple_patterns_any_match():
    sniffer = PacketSniffer([r"/api/.*", r"/graphql.*"])
    assert any(p.search("https://x.com/graphql?op=1") for p in sniffer._patterns) is True


def test_no_patterns_matches_nothing():
    sniffer = PacketSniffer([])
    assert any(p.search("https://x.com/anything") for p in sniffer._patterns) is False


# ---- drain ----

def test_drain_returns_empty_when_queue_empty():
    sniffer = PacketSniffer([r"x"])
    assert sniffer.drain() == []


def test_drain_returns_fifo_order():
    sniffer = PacketSniffer([r"x"])
    a = _cap("https://x.com/a", {"id": 1})
    b = _cap("https://x.com/b", {"id": 2})
    sniffer._queue.put_nowait(a)
    sniffer._queue.put_nowait(b)
    out = sniffer.drain()
    assert out == [a, b]


def test_drain_continues_after_queue_empty():
    sniffer = PacketSniffer([r"x"])
    sniffer._queue.put_nowait(_cap("https://x.com/a", {"id": 1}))
    first = sniffer.drain()
    second = sniffer.drain()
    assert len(first) == 1
    assert second == []


# ---- CapturedResponse.raw_size ----

def test_raw_size_returns_body_length():
    cap = _cap("https://x.com/a", {"id": 1})
    assert cap.raw_size == len(cap.body)
    assert cap.raw_size > 0


def test_raw_size_zero_for_empty_body():
    cap = CapturedResponse(url="https://x.com/a", status=200, headers={}, body=b"", json=None)
    assert cap.raw_size == 0


# ---- attach ----

def test_attach_registers_response_handler():
    sniffer = PacketSniffer([r"x"])
    page = MagicMock()
    sniffer.attach(page)
    page.on.assert_called_once_with("response", sniffer._on_response)


# ---- stream ----

async def test_stream_yields_items_in_order():
    sniffer = PacketSniffer([r"x"])
    a = _cap("https://x.com/a", {"id": 1})
    b = _cap("https://x.com/b", {"id": 2})
    sniffer._queue.put_nowait(a)
    sniffer._queue.put_nowait(b)

    gen = sniffer.stream()
    assert await gen.__anext__() is a
    assert await gen.__anext__() is b
    await gen.aclose()


# ---- _on_response ----

async def test_on_response_pushes_captured_response():
    sniffer = PacketSniffer([r"https://x\.com/.*"])
    response = _mock_response(
        url="https://x.com/api/users",
        status=200,
        headers={"content-type": "application/json"},
        body=b'{"id":1}',
        json_body={"id": 1},
    )
    await sniffer._on_response(response)
    drained = sniffer.drain()
    assert len(drained) == 1
    captured = drained[0]
    assert captured.url == "https://x.com/api/users"
    assert captured.status == 200
    assert captured.body == b'{"id":1}'
    assert captured.json == {"id": 1}


async def test_on_response_handles_missing_body():
    sniffer = PacketSniffer([r".*"])
    response = _mock_response(body_raises=True)
    await sniffer._on_response(response)
    assert sniffer.drain() == []


async def test_on_response_handles_binary_body():
    sniffer = PacketSniffer([r".*"])
    response = _mock_response(body=b"\x00\x01\x02", json_raises=True)
    await sniffer._on_response(response)
    drained = sniffer.drain()
    assert len(drained) == 1
    assert isinstance(drained[0].body, bytes)
    assert drained[0].body == b"\x00\x01\x02"
    assert drained[0].raw_size == 3
    assert drained[0].json is None


async def test_on_response_extracts_content_type_header():
    sniffer = PacketSniffer([r".*"])
    response = _mock_response(
        headers={"content-type": "application/json", "x-custom": "1"},
        body=b"{}",
        json_body={},
    )
    await sniffer._on_response(response)
    drained = sniffer.drain()
    assert len(drained) == 1
    assert drained[0].headers["content-type"] == "application/json"
    assert drained[0].headers["x-custom"] == "1"


async def test_on_response_pattern_filter_rejects_non_matching_url():
    sniffer = PacketSniffer([r"/api/.*"])
    response = _mock_response(url="https://x.com/static/img.png", body=b"x")
    await sniffer._on_response(response)
    assert sniffer.drain() == []


async def test_on_response_raw_size_matches_body_length():
    sniffer = PacketSniffer([r".*"])
    big_body = b"x" * 1000
    response = _mock_response(body=big_body, json_raises=True)
    await sniffer._on_response(response)
    drained = sniffer.drain()
    assert len(drained) == 1
    assert drained[0].raw_size == 1000
    assert drained[0].raw_size == len(drained[0].body)


async def test_status_code_passed_through():
    sniffer = PacketSniffer([r".*"])
    for status in (404, 500, 200):
        response = _mock_response(
            url=f"https://x.com/api/{status}",
            status=status,
            body=b"",
            json_raises=True,
        )
        await sniffer._on_response(response)
    drained = sniffer.drain()
    assert len(drained) == 3
    assert [d.status for d in drained] == [404, 500, 200]


async def test_on_response_queue_full_drops_item():
    sniffer = PacketSniffer([r".*"], max_size=1)
    r1 = _mock_response(url="https://x.com/a", body=b"first", json_raises=True)
    r2 = _mock_response(url="https://x.com/b", body=b"second", json_raises=True)
    await sniffer._on_response(r1)
    await sniffer._on_response(r2)
    drained = sniffer.drain()
    assert len(drained) == 1
    assert drained[0].body == b"first"


async def test_on_response_handles_json_decode_error():
    sniffer = PacketSniffer([r".*"])
    response = _mock_response(body=b"not json at all", json_raises=True)
    await sniffer._on_response(response)
    drained = sniffer.drain()
    assert len(drained) == 1
    assert drained[0].body == b"not json at all"
    assert drained[0].json is None


# ── M11: json.loads(body) not response.json() ────────────────────────────────

async def test_on_response_uses_body_bytes_for_json_not_response_json():
    """M11 — _on_response calls json.loads(body) not response.json()."""
    sniffer = PacketSniffer([r".*"])
    body_bytes = b'{"key": "value"}'
    response = _mock_response(body=body_bytes, json_body={"key": "value"})
    await sniffer._on_response(response)

    # response.json() (Playwright IPC call) must NOT have been called
    response.json.assert_not_called()

    drained = sniffer.drain()
    assert len(drained) == 1
    assert drained[0].json == {"key": "value"}
    assert drained[0].body == body_bytes


async def test_on_response_json_parse_catches_json_decode_error_not_all_exceptions():
    """M11 — JSON parse failure uses json.JSONDecodeError, item still captured."""
    sniffer = PacketSniffer([r".*"])
    # binary body that is not valid JSON
    response = _mock_response(body=b"\xff\xfe", json_raises=True)
    await sniffer._on_response(response)

    # response.json() must not be called — we use json.loads on the raw body
    response.json.assert_not_called()

    drained = sniffer.drain()
    assert len(drained) == 1
    assert drained[0].json is None


async def test_on_response_valid_json_parsed_from_body_bytes():
    """M11 — Valid JSON body bytes are parsed via json.loads."""
    sniffer = PacketSniffer([r".*"])
    payload = {"id": 42, "name": "test"}
    body_bytes = json.dumps(payload).encode()
    response = _mock_response(body=body_bytes, json_body=payload)
    await sniffer._on_response(response)

    # Must use body, not response.json()
    response.json.assert_not_called()

    drained = sniffer.drain()
    assert drained[0].json == payload
