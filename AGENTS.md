# AGENTS.md

Short project overview for AI coding agents and humans. See `CLAUDE.md`
for the source-of-truth tech stack, architecture tree, commands, and
invariants; see `PROBLEM.md` for the rationale.

## Project

`web-in-the-shell` — a headless, network-level AI agent (hackathon,
"Agentic Web" theme). It bypasses the GUI/DOM: a stealth headless
browser is run only to intercept raw JSON/API payloads; state is
distilled via Pydantic, fed to an LLM, and actions are executed via
raw HTTP requests using cached session credentials.

## Layout

- `src/main.py` — argparse CLI + interactive setup + pipeline driver +
  `--memory` subcommand. All session settings have a CLI flag.
- `src/network/` — `intercept/`, `session/`, `dispatch/`, `security/`
  (Playwright + httpx).
- `src/serialization/` — `CompactStateModel`, the Pydantic v2 noise
  stripper that runs between intercepts and the LLM.
- `src/ai/` — `provider.py` (Anthropic + any OpenAI-compatible
  endpoint), `discovery/planner.py`, `decision/executor.py`,
  `decision/recovery.py`.
- `src/persistence/` — sqlite-backed conversation memory
  (`Convo`, `ConvoMessage`, `ConvoStore`); `./wits.db` by default.
- `src/security/` — `sanitize`, `redact`, `allowlist`.
- `src/tui/` — Rich two-column display.
- `src/manager/`, `src/tester/`, `src/performance/` —
  documentation-only subagent definitions (no runtime code).
- `tests/unit/` + `tests/integration/` — `pytest`, `asyncio_mode = "auto"`,
  the integration tests are marked `@pytest.mark.integration`.

## Run

```bash
uv sync                                    # install deps
uv run playwright install chromium         # one-time browser binary

uv run src/main.py                         # interactive setup, then live pipeline
uv run src/main.py --mock                  # hardcoded 2-step plan, no API key
uv run src/main.py --no-interactive ...    # fully scripted, no prompts
uv run src/main.py --memory list           # inspect conversation memory

uv run pytest -m "not integration"         # fast unit suite (no browser)
uv run pytest                              # full suite
uv run ruff check src/                     # lint
```

See `uv run src/main.py --help` for every flag.

## Strict coding rules (mirror of CLAUDE.md)

- **No UI selectors.** No `click()`, no class targeting, no scrolling.
  The only exception is the optional `--login` handshake.
- **Pure async.** Browser, HTTP, LLM, and sqlite (via `asyncio.to_thread`)
  all flow through `async`/`await`.
- **Minimize before prompting.** Raw JSON → `CompactStateModel` →
  LLM. Never feed raw HTML, DOM dumps, or screenshots to the LLM.
- **Headers stay live.** Tokens refreshed mid-session are mirrored into
  the dispatch client at the next request.
- **Redact on write.** Every persisted message and result passes through
  `security.redact.redact()` before hitting disk.
- **No code comments** unless explicitly asked.
- Match the existing module's style (4-space indent, dataclasses,
  snake_case, light type hints).

## Tooling

- `uv` for env + deps. Python 3.12+ pinned via `.python-version`.
- `pytest` (+ `pytest-asyncio`, `pytest-cov`, `respx`) for tests.
- `ruff` for lint (`E`, `F`, `W`, `UP`).
- No formatter, no type checker.

## Build stage log

The project was built in seven waves. Stage-0 = the original
hackathon MVP. Stages 1–6 turned it into a testable, encrypted,
rate-limited, fully-flagged CLI with cross-process memory. The
same log is mirrored in `CLAUDE.md::Build stage log` so both
Claude Code and any other agent see the same picture.

### Stage 0 — original MVP

The initial commit already had: `main.py` argparse + mock plan,
`tui/display.py` Rich Live, `network/{security,intercept,session,
dispatch}` Playwright + httpx stack, `serialization/models.py`
`CompactStateModel`, `ai/{provider,discovery/planner,decision/
{executor,recovery}}` agents, `persistence/{db,models,store}.py`
stdlib-`sqlite3` `ConvoStore`, `security/{sanitize,redact,allowlist}.py`.

### Stage 1A — aiosqlite refactor

`ConvoStore` is now an `async` context manager on top of
`aiosqlite.connect()`. `asyncio.to_thread` + `asyncio.Lock` removed.
`Convo.from_row(row: aiosqlite.Row)` classmethod. `main.py` opens
`ConvoStore` in one `async with`. 16 tests in
`tests/unit/persistence/test_store.py`.

### Stage 1B — real CLI flags

Added `--target`, `--intent`, `--nav`, `--patterns`, `--replan`,
`--provider`, `--api-key`, `--model`, `--recovery-model`, `--login`,
`--no-interactive`, `--memory list|clear|clear-all`. Every session
setting has a flag; missing required fields fail fast under
`--no-interactive`.

### Stage 1C — pyproject metadata

`[project] name`, `version`, `description`, `authors`,
`requires-python`, `license`, `keywords`, `classifiers` added to
`pyproject.toml`.

### Stage 2D — in-process memory panel

`src/tui/memory.py::manage_memory(db_path, console, *,
input_provider=None, confirm_provider=None)`. Commands: `list`,
`view <intent>`, `clear <intent>`, `clear-all`, `help`,
`quit`/`q`/`exit`. `view`/`clear` JOIN all tail tokens. Catches
`EOFError`/`KeyboardInterrupt` silently. `SessionConfig.no_interactive`
skips the panel and runs `countdown_exit(5)` instead. 18 tests in
`tests/unit/tui/test_memory.py`.

### Stage 2E — session persistence

`src/persistence/session_store.py::SessionStore` async context
manager. Schema `sessions(host PK, cookies, bearer_token, csrf_token,
extra_headers, updated_at)`. `SessionManager.persist`/`restore` async
methods. **Restore is live-wins** (only fills empty fields). **Persist
gated on `has_material`** (only when at least one credential field has
material). `SessionStore` does **not** redact on write — round-trip is
the point. 23 tests across `test_session_store.py` (15) and
`test_session_manager_persistence.py` (8). `src/network/AGENT.md`
was created (was missing) and synced.

### Stage 2F — encryption at rest

`src/persistence/crypto.py` with `_load_or_create_key()` — keyring
primary, `Path.home() / ".wits" / "fernet.key"` 0600 fallback.
`encrypt(plaintext)` returns `_CIPHERTEXT_PREFIX + Fernet(...).encrypt(...)`
(`enc:v1:` sentinel). `decrypt` dispatches on prefix. `ConvoStore`
redacts-then-encrypts `messages`+`result`. `SessionStore` encrypts
`bearer_token`+`csrf_token` (per-field) and `cookies`+`extra_headers`
(per-JSON-blob). `keyring>=24.0.0` and `cryptography>=42.0.0` added.
15 tests in `tests/unit/persistence/test_crypto.py`.

### Stage 2G — rate limiting + 429 retry

`src/network/dispatch/ratelimit.py::TokenBucket` (async, monotonic-time
refill, busy-loop on `acquire`). `parse_retry_after(value) -> float`
parses seconds or HTTP-date. `DispatchClient` gains
`requests_per_second=5.0`, `burst=10`, `max_retries=1`; per-host bucket
behind `asyncio.Lock`; new `_do(method, url, **kwargs)` wraps the
httpx call: acquire → call → on 429 sleep `Retry-After` or
`2 ** attempt` and retry. `max_retries=0` disables retry. 14 tests
across `test_ratelimit.py` (8) and `test_dispatch_client.py` (6).

### Stage 3A — sniffer coverage

`tests/unit/network/test_sniffer.py` went 11 → 20 tests.
`sniffer.py` 66% → 100% covered. Tests use `AsyncMock` for
`response.body()` and `MagicMock` for the `Response` (no playwright
import).

### Stage 3B — TUI display coverage

`tests/unit/tui/test_display.py` went 29 → 52 tests.
`display.py` 79% → 98% covered. Color-mode tests use
`@patch("tui.display._NO_COLOR", False)` and `@patch("tui.display.Live")`.
`countdown_exit` uses `@patch("tui.display.asyncio.sleep", new_callable=AsyncMock)`.

### Stage 4 — integration fixes

Added `FormFieldStore` re-export to `src/persistence/__init__.py`
(was the source of the `ImportError` in
`tests/integration/test_e2e_harness.py`). Added
`tests/unit/persistence/test_forms.py` (26 tests, `forms.py` 0% → 100%).
Fixed 16 pre-existing ruff errors (F401 unused imports, E501 line
length, UP041 `asyncio.TimeoutError` → `TimeoutError`). Stabilised
the on-disk-ciphertext assertion in `test_store.py` (use aiosqlite
reader instead of stdlib `sqlite3.connect` to avoid WAL race; decrypt-
then-check instead of substring-on-ciphertext).

### Stage 5 — continuation polish

Fixed the save block in `src/main.py` so `--mock` mode also writes
a memory record (synthetic user + assistant messages from the
hardcoded plan + results). Without this fix, the entire persistence
story was unobservable without an API key. Added
`tests/integration/test_persistence_e2e.py` (subprocess run + assert
row in DB + Fernet ciphertext + plaintext intent + `ConvoStore`
round-trip). Added provider tests for `ImportError` re-raises
(lines 86–87, 92–93) and the openai re-raise / connect-error paths
(lines 142, 185). Provider coverage 95% → 100%. Fixed 8 more
pre-existing ruff errors in test files.

### Stage 6 — stage log + final coverage push

This section (the stage log itself) is mirrored in `CLAUDE.md::Build
stage log`. Coverage push for the remaining low-coverage files.
Final state: **all ruff checks pass**, **all tests pass**, **coverage
98.93%** (`main.py` omitted via `[tool.coverage.run] omit`).

## Verification (run any time)

```bash
uv run pytest --cov=src --cov-fail-under=90          # full suite
uv run pytest -m "not integration" --cov=src --cov-fail-under=90
uv run ruff check src/ tests/

# Persistence round-trip smoke test (network required)
WITS_DB_PATH=/tmp/wits-smoke.db NO_COLOR=1 \
    uv run src/main.py --mock --no-interactive --intent "Fetch posts then create one"
WITS_DB_PATH=/tmp/wits-smoke.db uv run src/main.py --memory list
WITS_DB_PATH=/tmp/wits-smoke.db uv run src/main.py --memory clear "Fetch posts then create one"
```
