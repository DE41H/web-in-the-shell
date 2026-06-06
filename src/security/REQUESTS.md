# Security Integration Requests

These are integration requests from the security module to other module owners.
Each request is a concrete, minimal change — no new dependencies, stdlib only.

---

## 1. `networking` → `src/network/dispatch/client.py`

**Request:** Call `validate_url(endpoint)` at the top of `get`, `post`, and `put`
before any request is dispatched. This blocks SSRF to RFC 1918, loopback, link-local,
cloud metadata endpoints, and non-HTTP(S) schemes.

```python
from security.allowlist import validate_url

async def get(self, endpoint: str, **kwargs) -> httpx.Response:
    validate_url(endpoint)   # raises ValueError on unsafe URLs
    ...

async def post(self, endpoint: str, payload: dict, **kwargs) -> httpx.Response:
    validate_url(endpoint)
    ...

async def put(self, endpoint: str, payload: dict, **kwargs) -> httpx.Response:
    validate_url(endpoint)
    ...
```

Let `ValueError` propagate to the caller (the AI executor should catch it and abort
the action, not swallow it).

---

## 2. `ai` / `serialization` → `src/serialization/models.py`

**Request:** Replace the existing minimal `sanitize()` helper inside `models.py` with
a call to `sanitize_for_llm()` from `src/security/sanitize.py` in `to_llm_context()`.
This adds control-character stripping and prompt-injection line removal on top of the
current null-byte + truncation logic.

```python
from security.sanitize import sanitize_for_llm

def to_llm_context(self) -> str:
    lines = [
        f"endpoint: {sanitize_for_llm(self.endpoint)}",
        f"status: {self.status_code}",
    ]
    for k, v in self.payload.items():
        lines.append(f"{sanitize_for_llm(str(k))}: {sanitize_for_llm(str(v))}")
    return "\n".join(lines)
```

The existing `sanitize()` function in `models.py` can then be deleted — `sanitize_for_llm`
is a strict superset.

---

## 3. `tui` → `src/tui/display.py`

**Request:** Wrap all user-visible strings with `redact()` from `src/security/redact.py`
before they are appended to the thought stream or displayed in the network monitor table.

Specifically:

- In `log_thought(self, thought: str)`: apply `redact(thought)` before appending.
- In `log_intercept(...)`: apply `redact(url)` on the stored URL so it does not appear
  in the ENDPOINT column unmasked.

```python
from security.redact import redact

def log_thought(self, thought: str) -> None:
    ts = datetime.now().strftime("%H:%M:%S")
    self._thoughts.append(f"[{ts}] {redact(thought)}")
    self._refresh()

def log_intercept(self, url: str, status: int, raw_bytes: int, compact_bytes: int) -> None:
    self._intercepts.append(
        {"url": redact(url), "status": status, "raw_bytes": raw_bytes, "compact_bytes": compact_bytes}
    )
    self._refresh()
```

Note: the TUI currently has no `_redact()` helper of its own. If one is added later,
confirm it covers Bearer tokens, JWTs, and `key=<16+alphanum>` patterns — the same
three covered by `src/security/redact.py`.
