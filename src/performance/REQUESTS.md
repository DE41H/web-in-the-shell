# Performance Agent — Requests & Notes

## Implemented changes

### Bounded queue in PacketSniffer (`src/network/intercept/sniffer.py`)

`max_size` was already present when the performance agent read the file (added by
the networking agent). The queue is bounded at 500 items (`asyncio.Queue(maxsize=500)`).
When the queue is full, new captures are silently dropped via `put_nowait` /
`QueueFull` — no blocking, no unbounded memory growth.

**Impact:** During long-running sessions that intercept thousands of responses before
the AI loop drains them, memory is now capped. Items arriving after the 500-item
threshold are discarded.

**Recommendation to the networking agent:** The `stream()` async generator already
added to `sniffer.py` is the right way to process captures in real-time rather than
batch-draining with `drain()`. The AI loop should consume `stream()` so the queue
stays near-empty and drop events remain rare.

### Concurrency semaphore in DispatchClient (`src/network/dispatch/client.py`)

`DispatchClient.__init__` now accepts `max_concurrent: int = 5` and creates
`self._sem = asyncio.Semaphore(max_concurrent)`. All three outbound methods (`get`,
`post`, `put`) acquire the semaphore before issuing the HTTP request.

**Impact:** No more than 5 requests can be in-flight simultaneously regardless of
how many coroutines call into the client concurrently. Existing callers are unaffected
because `max_concurrent` defaults to 5.

**Tuning:** If multi-step plans require higher throughput, pass a larger value at
construction time, e.g. `DispatchClient(session, max_concurrent=20)`.

### Connection pool limits in DispatchClient (`src/network/dispatch/client.py`)

`httpx.AsyncClient` is now created with explicit
`limits=httpx.Limits(max_connections=10, max_keepalive_connections=5)` instead of
httpx's default of 100 max connections. For a single-target agent, 10 connections is
more than sufficient and prevents accidental resource exhaustion on the target server.

---

## Requests to other agents

### Request to the `ai` agent

The executor's retry loop currently fires `post()` calls sequentially. If multi-step
plans are added in future, consider whether parallel step execution is desirable. If
it is, the semaphore in `DispatchClient` already handles the fan-out safely — just
call the methods concurrently (e.g. via `asyncio.gather`) and the semaphore will
throttle to `max_concurrent` in-flight requests automatically. No changes to
`client.py` are needed for that use-case.

### Note to the networking agent

`sniffer.py` already contained the `max_size` parameter and overflow-drop logic when
the performance agent read it, so no duplicate change was made. The `stream()` async
generator was also already present. No further action required from the networking
agent on this file regarding the bounded-queue feature.
