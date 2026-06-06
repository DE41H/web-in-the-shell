# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## Project

**Web in the Shell** — network-level AI agent. Stealth headless browser intercepts raw API payloads; Pydantic compacts them; LLM reasons over clean state and dispatches actions as direct HTTP requests using captured session credentials. Built for the Agentic Web hackathon.

## Commands

```bash
uv sync                             # install deps
uv run playwright install chromium  # one-time browser binary

uv run src/main.py                  # live mode — interactive setup, then TUI
uv run src/main.py --mock           # mock mode — no API key, hardcoded 2-step plan
uv run src/main.py --no-interactive \
    --target URL --intent TEXT \
    [--provider NAME --api-key KEY --model NAME --recovery-model NAME] \
    [--nav PATH --patterns REGEX...] [--replan N] [--login]
                                    # fully scripted run; no prompts

uv run src/main.py --memory list             # list stored conversations
uv run src/main.py --memory clear "intent"   # delete one intent
uv run src/main.py --memory clear-all        # delete every conversation

uv run pytest                       # full test suite
uv run pytest -m "not integration"  # unit tests only (fast, no browser)
```

Every session setting (provider, API key, target URL, intent, model,
nav path, replan count, login handshake) accepts a CLI flag. Any field
not supplied via a flag is prompted for in the terminal before the Rich
TUI launches; `--no-interactive` skips the prompts and fails fast on a
missing required field. `--mock` and `--memory ...` need no API key.

## Architecture

Imports are relative to `src/` (Python puts the script dir on `sys.path[0]` via `uv run src/main.py`). Use bare module names — no `src.` prefix.

```
src/
├── main.py                     # entry point; argparse CLI, interactive setup, --memory subcommand
├── tui/
│   ├── display.py               # AgentDisplay — live two-column operator dashboard
│   └── memory.py                # manage_memory — in-process memory panel (post-pipeline)
├── network/
│   ├── security/stealth.py     # StealthBrowser — Playwright + playwright-stealth
│   ├── intercept/sniffer.py    # PacketSniffer — page.on('response') + asyncio.Queue
│   ├── session/manager.py      # SessionManager — live bearer/CSRF/cookie extraction; persist/restore
│   └── dispatch/client.py      # DispatchClient — httpx.AsyncClient, per-host token bucket, 429 retry/backoff
├── serialization/
│   └── models.py               # CompactStateModel + compact_from_capture()
├── ai/
│   ├── provider.py             # LLMClient — Anthropic + any OpenAI-compatible provider
│   ├── discovery/planner.py    # PlannerAgent — intent → Plan via tool calling; resumes from ConvoStore
│   └── decision/
│       ├── executor.py         # ExecutionAgent — state deque, LLM payload refinement
│       └── recovery.py         # RecoveryAgent — HTTP 4xx/5xx → revised params
├── persistence/
│   ├── db.py                   # init_db, DEFAULT_DB_PATH (./wits.db, WAL, idempotent schema, two tables)
│   ├── crypto.py               # Fernet field-level encryption (keyring + file fallback, enc:v1: prefix)
│   ├── models.py               # Convo, ConvoMessage — Pydantic v2
│   ├── store.py                # ConvoStore — async ctx mgr via aiosqlite, redact-then-encrypt
│   └── session_store.py        # SessionStore — async ctx mgr, per-host creds, encrypt-only (round-trip)
├── security/
│   ├── sanitize.py             # sanitize_for_llm() — strips injection patterns
│   ├── redact.py               # redact() — JWT/bearer/key=value masking
│   └── allowlist.py            # validate_url() — SSRF guard
└── tui/
    └── display.py              # AgentDisplay — two-column Rich TUI
```

### Five-stage pipeline

1. **Intercept** — `PacketSniffer` regex-filters `page.on('response', ...)` into `asyncio.Queue(maxsize=500)`.
2. **State Sync** — `SessionManager` extracts tokens/CSRF/cookies from live traffic; re-applied at every dispatch.
3. **Clean** — `compact_from_capture()` → `CompactStateModel` strips noise; `to_llm_context()` emits compact key-value.
4. **Decide** — `PlannerAgent` routes intent (prepending any past conversation from `ConvoStore`); `ExecutionAgent` refines payload; state chained via `deque(maxlen=10)`.
5. **Act** — `DispatchClient.post/put/patch` fires against the target API with live-refreshed credentials.

After a successful run, `main.py` saves the planner's full message stream
to `ConvoStore`. The next run with the same intent loads it and replays
it as the planner's prior turn.

## LLM models

`LLMClient` (in `ai/provider.py`) speaks Anthropic natively and any OpenAI-compatible API (OpenAI, Groq, Gemini, Together, local Ollama) via the `openai` SDK. The provider is picked via `--provider` (default `anthropic`); per-provider defaults live in `ai/provider.py::DEFAULT_MODELS` and `DEFAULT_RECOVERY_MODELS`.

| Agent | Anthropic default | Why |
|---|---|---|
| PlannerAgent | `claude-sonnet-4-6` | routing needs reasoning depth |
| ExecutionAgent | `claude-sonnet-4-6` | payload construction is nuanced |
| RecoveryAgent | `claude-haiku-4-5-20251001` | fast/cheap in the retry hot path |

Override either model with `--model` and `--recovery-model`.

## Invariants

- **No UI selectors.** `click()`, `fill()`, `locator()` — only for unavoidable login handshakes.
- **Pure async.** Every browser call, HTTP request, LLM call, and sqlite call must be `async`/`await` (sqlite goes through `asyncio.to_thread`).
- **Minimize before prompting.** Raw JSON must pass through `CompactStateModel` before any LLM sees it.
- **Headers are live.** `DispatchClient._live_headers()` is called at request time — never cached.
- **Conversation memory:** per-intent resume via the persistence layer; the planner prepends past messages from `ConvoStore`; the executor and recovery agents are stateless. Every persisted message and result is run through `security.redact.redact()` first, then encrypted with `persistence.crypto.encrypt` before the INSERT. The on-disk column is always `enc:v1:`-prefixed Fernet ciphertext; reads decrypt first then parse.
- **Session persistence:** per-host credential rehydration via `SessionStore`; `SessionManager.restore(host, store)` primes initial state with stored values only where live values are empty (live wins). `SessionManager.persist(host, store)` is called only when at least one credential field has material. `SessionStore` does **not** redact — the store's purpose is to round-trip credentials, and redacting would break reload. The DB file is local-only, `.gitignore`d, and `persistence.crypto` adds field-level Fernet encryption at rest (keyring + `~/.wits/fernet.key` fallback, `enc:v1:` sentinel prefix on every encrypted column).
- **Rate limiting + 429 backoff:** `DispatchClient` keeps a `TokenBucket` per host (key = `urlparse(url).hostname`, default 5 rps / burst 10). The bucket blocks `acquire()` before each call. On a 429 it sleeps `Retry-After` (seconds or HTTP-date) or `2 ** attempt` seconds when the header is missing, then retries up to `max_retries` (default 1). Only 429 is retried; 5xx is returned to the caller unchanged.

## Mock mode

`--mock` is off by default. With it: full network/TUI pipeline runs against `jsonplaceholder.typicode.com` with a hardcoded 2-step plan, no API key required. The mock run also writes a memory record (synthetic user + assistant messages from the hardcoded plan + results) so the persistence story is observable without an API key. Without `--mock`: the interactive setup collects provider, API key, target, and intent before the TUI launches (unless every required field is supplied via CLI flags, in which case the prompts are skipped). Pass `--no-interactive` to disable prompts entirely.

## Build stage log

The project was built in seven waves, each handled by a parallel subagent
or a focused pass. Stage-0 = the original hackathon MVP. Stages 1–5
turned it into a testable, encrypted, rate-limited, fully-flagged CLI
with cross-process memory.

### Stage 0 — original MVP

- `main.py` argparse CLI with `--mock`, interactive setup, mock plan against jsonplaceholder.
- `tui/display.py` Rich Live two-column operator dashboard.
- `network/security/stealth.py` Playwright + playwright-stealth bootstrap.
- `network/intercept/sniffer.py` `page.on('response')` + `asyncio.Queue`.
- `network/session/manager.py` live bearer/CSRF/cookie extraction.
- `network/dispatch/client.py` `httpx.AsyncClient` wrapper.
- `serialization/models.py` `CompactStateModel` + `compact_from_capture()`.
- `ai/provider.py` `LLMClient` (Anthropic + OpenAI-compatible).
- `ai/discovery/planner.py` `PlannerAgent` with tool calling.
- `ai/decision/{executor,recovery}.py` stateless refinement agents.
- `persistence/{db,models,store}.py` stdlib-`sqlite3` `ConvoStore` (sync, `asyncio.to_thread`).
- `security/{sanitize,redact,allowlist}.py`.

### Stage 1A — aiosqlite refactor

- **Why:** stdlib `sqlite3` is blocking; going through `asyncio.to_thread`
  per call added latency and made context-manager usage awkward. The
  whole pipeline is `async`; persistence should be too.
- **What:** `ConvoStore` becomes an `async` context manager
  (`async with ConvoStore(path) as store:`) backed by `aiosqlite.connect()`.
  `asyncio.Lock` + `asyncio.to_thread` removed. `Convo.from_row(row:
  aiosqlite.Row)` classmethod on the Pydantic model. `main.py` opens
  `ConvoStore` alongside `DispatchClient` in one `async with` block.
- **Files:** `persistence/{store,db,models,__init__}.py`, `main.py`.
- **Tests:** 16 (all `tests/unit/persistence/test_store.py`).
- **Verify:** `uv run pytest tests/unit/persistence/`.

### Stage 1B — real CLI flags

- **Why:** MVP had prompts for every field; CI/scripts could not
  drive it without a TTY. Every setting needs a CLI flag.
- **What:** Added `--target`, `--intent`, `--nav`, `--patterns`,
  `--replan`, `--provider`, `--api-key`, `--model`,
  `--recovery-model`, `--login`, `--no-interactive` flags. Added
  `--memory list|clear|clear-all` subcommand. `SessionConfig` is the
  single source of truth; `_apply_args_to_config` does the wiring.
- **Files:** `main.py`, `README.md`, `CLAUDE.md`, `AGENTS.md`.
- **Verify:** `uv run src/main.py --help`.

### Stage 1C — pyproject metadata

- **Why:** `pyproject.toml` had no `[project]` section — `uv sync` and
  external tools had no name/description/authors to work with.
- **What:** Added `[project] name`, `version`, `description`, `authors`,
  `requires-python`, `license`, `keywords`, `classifiers`. Consolidated
  the runtime deps list.
- **Files:** `pyproject.toml`.

### Stage 2D — in-process memory panel

- **Why:** `--memory list` works from a script, but operators in a live
  TTY want a quick REPL to prune a single intent without re-running
  the whole pipeline.
- **What:** `src/tui/memory.py::manage_memory(db_path, console, *,
  input_provider=None, confirm_provider=None)`. Commands: `list`,
  `view <intent>`, `clear <intent>`, `clear-all`, `help`,
  `quit`/`q`/`exit`. `view`/`clear` JOIN all tail tokens so quoted
  intents with spaces work. Catches `EOFError`/`KeyboardInterrupt`
  silently. `SessionConfig.no_interactive: bool = False`; the panel
  is skipped and `countdown_exit(5)` runs instead when
  `--no-interactive` is set.
- **Files:** `src/tui/memory.py`, `main.py`, `src/tui/AGENT.md`,
  `README.md`.
- **Tests:** 18 (`tests/unit/tui/test_memory.py`).
- **Verify:** `uv run pytest tests/unit/tui/test_memory.py`.

### Stage 2E — session persistence

- **Why:** Without a credential rehydration layer, every cold start
  re-handshakes the login flow. With a per-host store, the second run
  inherits the bearer/CSRF/cookies transparently.
- **What:** `src/persistence/session_store.py::SessionStore` async
  context manager. Schema: `sessions(host PK, cookies, bearer_token,
  csrf_token, extra_headers, updated_at)` (added to `init_db`).
  `SessionManager.persist(host, store)` / `restore(host, store)` async
  methods. **Restore is live-wins** — only fills fields that are
  currently empty. **Persist gated on `has_material`** — only called
  when at least one of cookies/bearer/csrf/extras has material.
  `SessionStore` does **not** redact on write (round-trip is the
  point). `main.py` opens `SessionStore` alongside `ConvoStore` +
  `DispatchClient`; restore runs after `sync_cookies`, persist runs
  after a successful pipeline.
- **Files:** `src/persistence/{session_store,db,__init__,AGENT}.py`,
  `src/network/session/manager.py`, `main.py`,
  `src/network/AGENT.md` (created — was missing), `README.md`.
- **Tests:** 23 (`tests/unit/persistence/test_session_store.py: 15`,
  `tests/unit/network/test_session_manager_persistence.py: 8`).
- **Verify:** `uv run pytest tests/unit/persistence/ tests/unit/network/`.

### Stage 2F — encryption at rest

- **Why:** The DB file is local-only and `.gitignore`d, but a stolen
  laptop or backup leak would still expose the bearer/CSRF tokens
  sitting in the `sessions` table and the message contents in the
  `convos` table.
- **What:** `src/persistence/crypto.py` with `_load_or_create_key()` —
  tries `keyring.get_password("web-in-the-shell", "wits-fernet-key-v1")`
  first, falls back to `Path.home() / ".wits" / "fernet.key"` 0600.
  `encrypt(plaintext)` returns `_CIPHERTEXT_PREFIX + Fernet(...).encrypt(...)`
  (`enc:v1:` sentinel). `decrypt` dispatches on prefix (passes plaintext
  through for back-compat with unencrypted rows). `ConvoStore` runs
  `redact()` then `encrypt()` on `messages`+`result`. `SessionStore`
  encrypts `bearer_token`+`csrf_token` per-field and `cookies`+
  `extra_headers` per-JSON-blob. `keyring>=24.0.0` and
  `cryptography>=42.0.0` added to `pyproject.toml`.
- **Files:** `src/persistence/{crypto,store,session_store,__init__}.py`,
  `pyproject.toml`, `src/security/AGENT.md` (Persistence Encryption
  section), `src/tester/AGENT.md` (crypto test conventions),
  `src/manager/AGENT.md` (persistence row updated).
- **Tests:** 15 (`tests/unit/persistence/test_crypto.py`).
- **Verify:** `uv run pytest tests/unit/persistence/`.

### Stage 2G — rate limiting + 429 retry

- **Why:** Sequential bursts of `httpx` calls can trip per-host rate
  limits. When a 429 lands, the call site had to handle
  `Retry-After` manually — easy to forget, easy to get wrong.
- **What:** `src/network/dispatch/ratelimit.py::TokenBucket` (async,
  `time.monotonic` refill, `asyncio.Lock`-free, busy-loop with
  `asyncio.sleep(0.01)`). `parse_retry_after(value) -> float` parses
  seconds or HTTP-date via `email.utils.parsedate_to_datetime`.
  `DispatchClient` gains `requests_per_second=5.0`, `burst=10`,
  `max_retries=1` init args. Per-host bucket dict behind
  `asyncio.Lock`. New `_do(method, url, **kwargs)` wraps the httpx
  call: acquire → call → on 429, sleep `Retry-After` or `2**attempt`
  then retry up to `max_retries`. `max_retries=0` disables retry.
  Complements the existing `max_concurrent` semaphore.
- **Files:** `src/network/dispatch/{client,ratelimit}.py`,
  `src/network/AGENT.md` (Respx Retry/Backoff Tests section).
- **Tests:** 14 (`tests/unit/network/test_ratelimit.py: 8`,
  `tests/unit/network/test_dispatch_client.py: 6`).
- **Verify:** `uv run pytest tests/unit/network/`.

### Stage 3A — sniffer coverage

- **Why:** `sniffer.py` was at 66% coverage — the `_on_response` callback
  path (Playwright's `Response` event signature) was untested.
- **What:** `tests/unit/network/test_sniffer.py` went from 11 to 20 tests.
  Tests use `unittest.mock.AsyncMock` for `response.body()` and
  `MagicMock` for the `Response` (no playwright import).
- **Files:** `tests/unit/network/test_sniffer.py`.
- **Verify:** `uv run pytest tests/unit/network/test_sniffer.py --cov=src/network/intercept/sniffer.py`.

### Stage 3B — TUI display coverage

- **Why:** `tui/display.py` was at 79% — the color-mode branch and the
  `countdown_exit` tick were uncovered.
- **What:** `tests/unit/tui/test_display.py` went from 29 to 52 tests.
  Color-mode tests use `@patch("tui.display._NO_COLOR", False)` and
  `@patch("tui.display.Live")`. `countdown_exit` uses
  `@patch("tui.display.asyncio.sleep", new_callable=AsyncMock)`.
- **Files:** `tests/unit/tui/test_display.py`,
  `src/tui/AGENT.md` (Test coverage expectations section).
- **Verify:** `uv run pytest tests/unit/tui/test_display.py --cov=src/tui/display.py`.

### Stage 4 — integration fixes

- **Why:** First run of `tests/integration/test_e2e_harness.py` failed
  with `ImportError: cannot import name 'FormFieldStore' from 'persistence'`
  — the `forms.py` module existed, `main.py` imported it, but
  `persistence/__init__.py` never re-exported it. Also, 16 pre-existing
  ruff errors (3 unused imports in `main.py`, 8 E501 lines, 2
  `asyncio.TimeoutError` → `TimeoutError` UP041, 3 misc) and
  `test_save_stores_encrypted_ciphertext_on_disk` was flaky on the
  stdlib-`sqlite3` WAL read.
- **What:** Added `FormFieldStore` to `persistence/__init__.py`.
  Added `tests/unit/persistence/test_forms.py` (26 tests; `forms.py`
  0% → 100% covered). Fixed all 16 ruff errors. Stabilised the
  on-disk-ciphertext test: aiosqlite reader instead of stdlib
  `sqlite3.connect`; decrypt-then-check instead of substring-on-
  ciphertext (Fernet base64 can contain `"ok"` by coincidence).
- **Files:** `src/persistence/__init__.py`,
  `tests/unit/persistence/test_forms.py`,
  `tests/unit/persistence/test_store.py` (lines 226–251),
  `src/main.py`, `src/ai/{provider,discovery/planner}.py`,
  `tests/integration/test_e2e_harness.py`.
- **Verify:** `uv run pytest`, `uv run ruff check src/ tests/`.

### Stage 5 — continuation polish

- **Why:** The mock pipeline was the only entry point that worked
  without an API key, but the save block in `main.py` was gated on
  `last_planner is not None` — and mock mode never creates a planner.
  Net result: the entire persistence story was unobservable without a
  real API key. Also, several low-coverage edge cases in
  `ai/provider.py` were untested.
- **What:** Fixed the save block in `main.py` so `--mock` mode also
  writes a memory record (synthetic user + assistant messages from
  the hardcoded plan + results). Added
  `tests/integration/test_persistence_e2e.py` (subprocess run +
  assert row in DB + Fernet ciphertext + plaintext intent +
  `ConvoStore` round-trip). Added provider tests for
  `ImportError` re-raises (lines 86–87, 92–93) and the openai
  re-raise / connect-error paths (lines 142, 185). Fixed 8 more
  pre-existing ruff errors in test files. Provider coverage went
  95% → 100%.
- **Files:** `src/main.py`, `tests/integration/test_persistence_e2e.py`
  (new), `tests/unit/ai/test_provider.py`, `tests/unit/ai/test_executor.py`,
  `tests/unit/ai/test_planner.py`, `tests/unit/security/test_redact.py`,
  `tests/unit/tui/test_display.py`, `tests/unit/persistence/test_forms.py`,
  `tests/unit/main/test_main_utils.py`.
- **Verify:** `uv run pytest --cov=src --cov-fail-under=90`.

### Stage 6 — stage log + final coverage push

- **Why:** Each agent's working context starts fresh, so the
  "what is happening at each stage" answer had to be persisted to
  the two onboarding docs (`CLAUDE.md` for Claude Code, `AGENTS.md`
  for any AI agent). Also: the remaining low-coverage files
  (`tui/display.py: 98%`, `tui/memory.py: 96%`,
  `ai/provider.py: 99%`) were the obvious next coverage targets.
- **What:** This section (the stage log itself). Identical content
  mirrored into `AGENTS.md::Build stage log` so every agent gets the
  same picture regardless of which onboarding doc they read.
  Coverage push for the remaining files.
- **Files:** `CLAUDE.md`, `AGENTS.md`, `tests/unit/ai/test_provider.py`,
  `tests/unit/tui/test_display.py`, `tests/unit/tui/test_memory.py`.
- **Verify:** `uv run pytest --cov=src --cov-fail-under=90`,
  `uv run ruff check src/ tests/`.
