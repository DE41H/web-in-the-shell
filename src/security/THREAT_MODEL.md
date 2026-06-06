# Threat Model — Web in the Shell

_Last updated: 2026-06-06_

## Threat Table

| Threat | Severity | Mitigation | Status |
|---|---|---|---|
| Prompt injection via captured API payloads | High | `sanitize_for_llm()` in `sanitize.py` | Mitigated — awaiting integration in `serialization/models.py` and `ai/` |
| SSRF via LLM-generated URLs | Critical | `validate_url()` in `allowlist.py` | Mitigated — awaiting integration in `networking/dispatch/client.py` |
| Token/cookie leakage via logs or TUI | High | `redact()` in `redact.py` | Mitigated — awaiting integration in `tui/display.py` |
| Session fixation across targets | Medium | Reset `SessionCredentials` between sessions | Open — no enforcement yet in `session/manager.py` |
| Stealth browser fingerprinting / CSP bypass | Low (accepted) | `bypass_csp=True` scoped only to interception context; documented below | Accepted risk |

---

## Notes per Threat

### Prompt Injection
Captured API payloads are attacker-controlled JSON that reaches the LLM as plain text.
A malicious server can embed `Ignore previous instructions` or `System: exfiltrate cookies`
inside a JSON value. `sanitize_for_llm()` strips control characters and drops lines matching
known injection openers before any payload is forwarded to the model.

### SSRF
The agent fires raw HTTP requests to URLs derived from LLM output and user intent.
An adversary can craft a response that causes the LLM to emit an internal URL
(`http://169.254.169.254/latest/meta-data/`). `validate_url()` raises `ValueError` for
RFC 1918 addresses, loopback, link-local, cloud metadata endpoints, and non-HTTP(S) schemes.
This guard must run at every call-site in `DispatchClient` (`get`, `post`, `put`).

### Token / Cookie Leakage
Bearer tokens and session cookies surfaced in the Rich TUI thought-stream or Python logs
can be harvested from terminal scrollback, screenshots, or CI artefacts.
`redact()` replaces Bearer values, JWT-shaped strings, and long `key=value` secrets with
`[REDACTED]` / `[JWT REDACTED]` before any string is displayed or logged.

### Session Fixation
`SessionCredentials` loaded during one target's login handshake must not persist into a
subsequent unrelated session. If the same `SessionManager` instance is reused across
targets without credential reset, a CSRF token or bearer token from site A is silently
sent to site B. Fix: call `session.reset()` (or reinitialise `SessionManager`) between
distinct target sessions.

### Stealth Browser / `bypass_csp=True`
`bypass_csp=True` in `StealthBrowser` disables the Content-Security-Policy enforcement
for pages the agent loads. This is intentional — it allows Playwright's network
interception hooks to capture requests that would otherwise be blocked by a strict CSP.
The risk (a rogue page executing injected scripts without CSP constraint) is accepted
because the browser is headless and never renders content for a human. The tradeoff
must not be extended beyond the interception context; the login handshake browser also
sets `bypass_csp=True` and should be reviewed if that window expands.
