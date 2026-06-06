```
 ██╗    ██╗███████╗██████╗     ██╗███╗   ██╗    ████████╗██╗  ██╗███████╗
 ██║    ██║██╔════╝██╔══██╗    ██║████╗  ██║    ╚══██╔══╝██║  ██║██╔════╝
 ██║ █╗ ██║█████╗  ██████╔╝    ██║██╔██╗ ██║       ██║   ███████║█████╗
 ██║███╗██║██╔══╝  ██╔══██╗    ██║██║╚██╗██║       ██║   ██╔══██║██╔══╝
 ╚███╔███╔╝███████╗██████╔╝    ██║██║ ╚████║       ██║   ██║  ██║███████╗
  ╚══╝╚══╝ ╚══════╝╚═════╝     ╚═╝╚═╝  ╚═══╝       ╚═╝   ╚═╝  ╚═╝╚══════╝

 ███████╗██╗  ██╗███████╗██╗     ██╗
 ██╔════╝██║  ██║██╔════╝██║     ██║
 ███████╗███████║█████╗  ██║     ██║
 ╚════██║██╔══██║██╔══╝  ██║     ██║
 ███████║██║  ██║███████╗███████╗███████╗
 ╚══════╝╚═╝  ╚═╝╚══════╝╚══════╝╚══════╝
```

> **A network-level AI agent that abandoned the browser's visual layer entirely.**
> It doesn't look at webpages. It *listens* to them.

---

## The Problem with Every Other Web Agent

Every AI web agent you've seen works the same way: load a page, parse the DOM, stare at a screenshot, click a button, pray it didn't move. This works fine for demos. It fails in production.

The modern web has three properties that make this approach structurally broken:

**UI Churn.** Dynamic CSS-in-JS frameworks (Tailwind, styled-components) regenerate class names on every deployment. A single layout tweak breaks every selector, every bounding box, every visual cue your agent was trained on. The frontend is a moving target by design.

**The Canvas/Wasm Black Box.** Figma. Google Sheets. Interactive dashboards. Crypto dApps. These applications render on an HTML5 `<canvas>` or execute logic in WebAssembly bytecode. To a DOM-based agent, they don't exist. There are no nodes to read, no buttons to click — just an opaque pixel canvas. Traditional automation is completely blind here.

**Token & Latency Waste.** Feeding a raw HTML dump into an LLM burns 20,000+ tokens of tracking pixels, layout styles, and telemetry scripts before a single meaningful byte of data appears. Waiting for full-page renders, image loads, and CSS layout computation adds seconds of dead latency to every step.

---

## The Architecture: Operating at the Network Layer

**Web in the Shell** abandons the DOM entirely. Instead of looking at what the browser renders, it intercepts what the server actually sends.

The browser becomes a passive credential harvester — it handles authentication so we can inherit the session, then gets out of the way. All actual intelligence runs against raw API streams.

```
┌──────────────────────────────────────────────────────────────────────┐
│                           PIPELINE                                     │
├──────────┬──────────────┬──────────┬──────────┬──────────────────────┤
│          │              │          │          │                        │
│ INTERCEPT│  STATE SYNC  │  CLEAN   │  DECIDE  │         ACT           │
│          │              │          │          │                        │
│ Headless │ Cache auth   │ Pydantic │  LLM +   │ Raw HTTP POST/PUT     │
│ browser  │ tokens, CSRF │ schemas  │ function │ with captured         │
│ listens  │ cookies from │ strip UI │ calling  │ session credentials   │
│ on       │ live traffic │ bloat    │ tools    │                        │
│ response │              │          │          │                        │
│ streams  │              │          │          │                        │
└──────────┴──────────────┴──────────┴──────────┴──────────────────────┘
```

### Stage 1 — Intercept
`page.on('response', ...)` with regex pattern matching. The browser runs in stealth mode (`playwright-stealth`) to defeat bot-detection fingerprinting. Intercepts feed a bounded `asyncio.Queue` in real-time; the TUI displays each capture as it arrives.

### Stage 2 — State Sync
Every outgoing request and incoming response is scanned for session tokens, CSRF headers, and cookies. These are kept live — if the application rotates a token mid-session, we catch it off the wire and patch our HTTP client in real time. The agent's session never goes stale.

### Stage 3 — Clean
Raw API responses are collapsed through Pydantic v2 `CompactStateModel` — a noise-stripping schema that strips 30+ telemetry/UI key categories before anything touches the LLM. Token cost drops by 95–99%.

### Stage 4 — Decide
The minimized state snapshot is passed to the LLM with a set of tool signatures representing available actions. The LLM reasons over *data*, not pixels. It returns a structured tool call: which action, which endpoint, which parameters.

### Stage 5 — Act
The chosen action fires as a direct HTTP request using the captured session credentials — no browser involvement. `POST`, `PUT`, `PATCH` — at machine speed against the application's private API, indistinguishable from the client's own traffic.

---

## Why This Wins

| Dimension | GUI/DOM Agents | Web in the Shell |
|---|---|---|
| Fragility | Breaks on every frontend deploy | API contracts change orders of magnitude less frequently |
| Canvas/Wasm apps | Completely blind | Irrelevant — we read the data layer, not the render layer |
| Tokens per step | 10,000–50,000+ (raw HTML/screenshots) | ~50–500 (distilled Pydantic schema) |
| Latency per action | 2–10s (full page render cycle) | ~50–200ms (direct HTTP) |
| Session resilience | Static cookies, breaks on token rotation | Dynamic header sync from live traffic |

---

## Stack

| Layer | Technology |
|---|---|
| Package & env manager | [`uv`](https://github.com/astral-sh/uv) |
| Runtime | Python 3.12+ (strictly async) |
| Browser / network tap | Playwright + `playwright-stealth` |
| State distillation | Pydantic v2 |
| HTTP execution | `httpx.AsyncClient` with per-host token-bucket rate limiting and 429 retry/backoff |
| LLM orchestration | Provider-agnostic — Anthropic, OpenAI, Groq, Gemini, Together, or local Ollama; native function/tool calling |
| Conversation memory | SQLite (WAL) via `aiosqlite`; per-intent resume; in-process memory panel |
| Session persistence | SQLite (WAL) via `aiosqlite`; per-host credential rehydration; **not** redacted on write (the store's whole point is to round-trip tokens); field-level Fernet encryption at rest (keyring-backed key, `enc:v1:` prefix) |
| Terminal UI | Rich (two-column live layout) |
| Security | SSRF allowlist, prompt-injection sanitiser, credential redaction |

---

## Setup

```bash
# Install uv if you don't have it
curl -LsSf https://astral.sh/uv/install.sh | sh

# Sync the environment
uv sync

# Install Playwright's Chromium
uv run playwright install chromium
```

---

## Running

```bash
# Full pipeline with the hardcoded 2-step plan — no API key required.
uv run src/main.py --mock

# Live LLM mode — interactive setup prompts for provider, key, target, intent.
uv run src/main.py

# Same thing, fully scripted (no prompts):
ANTHROPIC_API_KEY=sk-... uv run src/main.py \
  --no-interactive \
  --target https://api.example.com \
  --intent "Create a new draft post titled Hello World" \
  --nav    /api/posts \
  --patterns "/api/posts" "/api/users"

# Pick a different provider and model:
uv run src/main.py \
  --provider groq \
  --model    llama-3.3-70b-versatile \
  --api-key  $GROQ_API_KEY

# Sites that require a human login first:
uv run src/main.py --login --target https://app.example.com
```

### CLI reference

| Flag | Default | Description |
|---|---|---|
| `--target URL` | `https://jsonplaceholder.typicode.com` | Base URL of the target application |
| `--intent TEXT` | `Fetch posts, then create a new post titled 'Agent Test'` | Natural-language goal for the agent |
| `--nav PATH` | `/posts` | Navigation path appended to `--target` to trigger API captures |
| `--patterns REGEX...` | `/posts /todos /users /comments` | URL regex patterns to intercept |
| `--replan N` | `2` | Max replanning attempts on execution failure |
| `--provider NAME` | `anthropic` | LLM provider: `anthropic`, `openai`, `groq`, `gemini`, `together`, `ollama` |
| `--api-key KEY` | _provider env var_ | LLM API key; falls back to `ANTHROPIC_API_KEY` / `OPENAI_API_KEY` / `GROQ_API_KEY` / `GEMINI_API_KEY` / `TOGETHER_API_KEY` (ollama needs no key) |
| `--model NAME` | provider default | Primary model used by the planner and executor |
| `--recovery-model NAME` | provider default | Cheap/fast model used by the recovery hot path |
| `--login` | off | Open a visible browser for a manual login handshake |
| `--mock` | off | Hardcoded 2-step plan, no LLM call, no API key required |
| `--no-interactive` | off | Skip every prompt; fail if a required field is missing |
| `--memory CMD [ARG ...]` | _disabled_ | Conversation-memory subcommand — see below |

The default flow is interactive: any field not supplied as a flag is prompted
for in the terminal before the Rich TUI starts. Pass `--no-interactive` to
skip prompts entirely; pass `--mock` to skip both prompts and the LLM call.

### Conversation memory

Successful runs save the planner's full message stream to a local SQLite
database (`./wits.db` by default, overridable with `WITS_DB_PATH`). The next
time you run the same intent, the planner prepends the prior turn to its
context — past API choices and parameter shapes carry across runs. Every
message and result is run through the credential-redactor before it hits the
disk; no raw tokens are persisted. `--mock` runs also write a synthetic
memory record (user + assistant messages synthesised from the hardcoded
plan + results) so the persistence story is observable without an API key.

The `messages` and `result` columns (and every column in the per-host session
store: `bearer_token`, `csrf_token`, `cookies`, `extra_headers`) are stored
as Fernet ciphertext with an `enc:v1:` sentinel prefix. The 32-byte key is
held in the OS keyring (`web-in-the-shell / wits-fernet-key-v1`); if the
keyring backend is unavailable, a fallback is written to
`~/.wits/fernet.key` with `0600` perms. The plaintext shape is restored
transparently on read, so the live API is unchanged — a `Convo` loaded
from disk and a `SessionCredentials` loaded from disk are
indistinguishable from the in-memory originals.

The `--memory` subcommand inspects and clears the store from a script. It
does not run the pipeline and does not require an LLM API key:

```bash
uv run src/main.py --memory list                    # list stored conversations
uv run src/main.py --memory clear "Fetch posts…"    # delete one intent
uv run src/main.py --memory clear-all               # delete every conversation
```

When the pipeline runs interactively (i.e. `--no-interactive` is NOT set), an
in-process memory panel opens right after the run finishes. It accepts the
same commands (`list`, `view <intent>`, `clear <intent>`, `clear-all`,
`help`, `quit`) at a `memory>` prompt — useful for pruning a single intent
before re-running the pipeline, or auditing the messages that will be
prepended on the next run.

---

## Design Constraints

These are invariants, not preferences.

- **No UI selectors.** No `click()`, no CSS class targeting, no scrolling. The only exception is an initial login handshake when a site has no headless auth alternative.
- **Pure async everywhere.** Every browser call, network request, and LLM interaction is `async`/`await`. Blocking I/O anywhere in the pipeline breaks the concurrency model.
- **Minimize before prompting.** A raw network response never touches the LLM. It is always serialized through a Pydantic schema first.
- **Headers stay live.** The HTTP client's session headers are updated dynamically from intercepted traffic. A token that rotated 30 seconds ago is already applied.
- **Rate-limited dispatch.** `DispatchClient` keeps a per-host token bucket (default 5 rps / burst 10) and respects `Retry-After` on a 429 (seconds or HTTP-date), with exponential backoff (`2 ** attempt`) when the header is missing. Other status codes are returned to the caller unchanged.

---

*Built for the Agentic Web hackathon.*
