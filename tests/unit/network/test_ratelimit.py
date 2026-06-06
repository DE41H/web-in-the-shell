import asyncio
import time

import pytest

from network.dispatch.ratelimit import TokenBucket, parse_retry_after


async def test_bucket_starts_full():
    bucket = TokenBucket(capacity=5, refill_per_second=1.0)
    assert bucket.available() == 5.0


async def test_acquire_consumes_one_token():
    bucket = TokenBucket(capacity=3, refill_per_second=10.0)
    await bucket.acquire()
    assert bucket.available() == pytest.approx(2.0, abs=0.1)


async def test_acquire_blocks_when_empty():
    bucket = TokenBucket(capacity=1, refill_per_second=2.0)
    await bucket.acquire()
    start = time.monotonic()
    await bucket.acquire()
    elapsed = time.monotonic() - start
    assert elapsed >= 0.4


async def test_acquire_refills_over_time():
    bucket = TokenBucket(capacity=5, refill_per_second=10.0)
    await bucket.acquire()
    await bucket.acquire()
    await asyncio.sleep(0.2)
    assert bucket.available() == pytest.approx(5.0, abs=0.1)


def test_parse_retry_after_seconds():
    assert parse_retry_after("5") == 5.0
    assert parse_retry_after("0.25") == 0.25


def test_parse_retry_after_http_date():
    assert parse_retry_after("Wed, 21 Oct 2099 07:28:00 GMT") > 1_000_000
    assert parse_retry_after("Wed, 21 Oct 2000 07:28:00 GMT") == 0.0
    assert parse_retry_after("Wed, 21 Oct 2099 07:28:00") > 1_000_000


def test_parse_retry_after_none_returns_zero():
    assert parse_retry_after(None) == 0.0
    assert parse_retry_after("") == 0.0


def test_parse_retry_after_invalid_returns_zero():
    assert parse_retry_after("not-a-date-or-number") == 0.0


def test_parse_retry_after_is_sync_not_coroutine():
    """M6 — parse_retry_after must be a plain function, not async."""
    import inspect
    result = parse_retry_after("1")
    assert not inspect.iscoroutine(result), "parse_retry_after must not be async"
    assert result == 1.0


def test_parse_retry_after_zero_returns_zero_not_negative():
    """M7 — Retry-After: 0 means retry immediately; must return 0.0."""
    assert parse_retry_after("0") == 0.0
    assert parse_retry_after("0.0") == 0.0
