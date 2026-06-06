"""Per-host rate limiting and 429 Retry-After parsing for DispatchClient."""

import asyncio
import time
from datetime import UTC, datetime
from email.utils import parsedate_to_datetime


class TokenBucket:
    """Async token bucket: capacity + refill rate per second.

    The bucket starts full and refills continuously. ``acquire`` blocks
    the calling coroutine until enough tokens are available. The internal
    lock guards the refill/consume critical section; the sleep that
    follows a missed ``acquire`` is performed *outside* the lock so
    multiple waiters on the same host do not serialise.
    """

    def __init__(self, capacity: int, refill_per_second: float) -> None:
        self.capacity = capacity
        self.refill_per_second = refill_per_second
        self._tokens = float(capacity)
        self._last_refill = time.monotonic()
        self._lock = asyncio.Lock()

    def _refill(self) -> None:
        now = time.monotonic()
        elapsed = now - self._last_refill
        if elapsed > 0:
            self._tokens = min(self.capacity, self._tokens + elapsed * self.refill_per_second)
            self._last_refill = now

    def available(self) -> float:
        self._refill()
        return self._tokens

    async def acquire(self, tokens: int = 1) -> None:
        while True:
            wait = 0.0
            async with self._lock:
                self._refill()
                if self._tokens >= tokens:
                    self._tokens -= tokens
                    return
                needed = tokens - self._tokens
                wait = max(needed / self.refill_per_second, 0.01)
            await asyncio.sleep(wait)


def parse_retry_after(value: str | None) -> float:
    """Parse a ``Retry-After`` header value. Return seconds to wait (>= 0).

    Accepts a non-negative integer/float (seconds) or an RFC 7231
    HTTP-date. Returns 0.0 for ``None``, empty, or unparseable input.
    A value of 0.0 means retry immediately (server said Retry-After: 0).
    """
    if not value:
        return 0.0
    text = value.strip()
    try:
        return max(0.0, float(text))
    except ValueError:
        pass
    try:
        target = parsedate_to_datetime(text)
    except (TypeError, ValueError, IndexError):
        return 0.0
    if target.tzinfo is None:
        target = target.replace(tzinfo=UTC)
    delta = (target - datetime.now(UTC)).total_seconds()
    return max(0.0, delta)
