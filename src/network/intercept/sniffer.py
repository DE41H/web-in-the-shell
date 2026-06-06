import asyncio
import re
from dataclasses import dataclass, field

from playwright.async_api import Page, Response


@dataclass
class CapturedResponse:
    url: str
    status: int
    headers: dict[str, str]
    body: bytes = field(default_factory=bytes)
    json: dict | list | None = None

    @property
    def raw_size(self) -> int:
        return len(self.body)


class PacketSniffer:
    """Filters page responses by URL regex and queues matching payloads."""

    def __init__(self, patterns: list[str], max_size: int = 500) -> None:
        self._patterns = [re.compile(p) for p in patterns]
        self._queue: asyncio.Queue[CapturedResponse] = asyncio.Queue(maxsize=max_size)

    async def _on_response(self, response: Response) -> None:
        if not any(p.search(response.url) for p in self._patterns):
            return
        try:
            body = await response.body()
            try:
                json_body = await response.json()
            except Exception:
                json_body = None
            captured = CapturedResponse(
                url=response.url,
                status=response.status,
                headers=dict(response.headers),
                body=body,
                json=json_body,
            )
            try:
                self._queue.put_nowait(captured)
            except asyncio.QueueFull:
                pass  # queue is full; drop the new item rather than blocking
        except Exception:
            pass  # body unavailable for redirects, streams, etc.

    def attach(self, page: Page) -> None:
        page.on("response", self._on_response)

    def drain(self) -> list[CapturedResponse]:
        """Return all currently buffered items without waiting. Kept for backward compatibility."""
        results = []
        while not self._queue.empty():
            results.append(self._queue.get_nowait())
        return results

    async def stream(self):
        """Async generator that yields CapturedResponse items as they arrive.

        Prefer this over polling drain() in a tight loop; it suspends until the
        next item is available rather than spinning.
        """
        while True:
            yield await self._queue.get()
