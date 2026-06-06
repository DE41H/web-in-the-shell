# Cross-boundary requests from `network`

These are changes that the networking layer needs from other modules. The
network team will not touch files outside `src/network/`; owners of the
relevant modules should pick these up and confirm the interface before the
next integration milestone.

---

## Request to: security

**Need:** A `validate_url(url: str) -> str` function that raises `ValueError`
for any URL that resolves to an SSRF-dangerous destination: `localhost`,
`127.0.0.0/8`, RFC 1918 ranges (`10.x`, `172.16–31.x`, `192.168.x`),
link-local (`169.254.x.x`), IPv6 loopback (`::1`), and the GCP/AWS metadata
endpoints (`metadata.google.internal`, `169.254.169.254`).

**Why:** `DispatchClient.get/post/put` must call this before firing any
outbound HTTP request to prevent the agent from being weaponised as an SSRF
proxy when the AI-decided target URL is attacker-controlled.

**Suggested interface:**

```python
# src/security/ssrf.py  (or wherever the security module lives)
def validate_url(url: str) -> str:
    """
    Return `url` unchanged if it is safe to reach from the agent host.
    Raise ValueError with a human-readable message if the resolved address
    falls in a blocked range.

    Resolution must happen at call time (not import time) so dynamic DNS is
    also covered. The caller should pass the raw URL string; this function
    is responsible for parsing and resolving.
    """
```

Usage in `DispatchClient` will be:

```python
from security.ssrf import validate_url  # import path TBC

async def get(self, endpoint: str, **kwargs) -> httpx.Response:
    validate_url(self._base_url + endpoint)
    ...
```

---

## Request to: tui

**Need:** Confirmation that the `log_intercept` function signature below is
stable and will not change without notice to the networking team.

**Why:** `PacketSniffer.stream()` will call `log_intercept` from its consumer
loop (see `src/network/intercept/sniffer.py`) to surface each captured
response in the terminal UI. If the signature shifts, the networking layer
will silently drop log calls or crash at runtime.

**Suggested interface:**

```python
def log_intercept(
    url: str,
    status: int,
    raw_bytes: int,
    compact_bytes: int,
) -> None:
    """
    Display one intercepted response in the TUI.

    url          — full request URL as captured by Playwright
    status       — HTTP status code (e.g. 200, 401, 304)
    raw_bytes    — size of the raw body in bytes
    compact_bytes — size after Pydantic compaction (0 if not yet compacted)
    """
```

Please confirm whether this is already implemented, whether `compact_bytes`
should be optional (`int | None = None`), and what module path we should
import it from.
