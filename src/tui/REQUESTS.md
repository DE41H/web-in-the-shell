# TUI module — inter-module requests & call-signature reference

This file documents the public API additions made to `AgentDisplay` so that
callers in `src/main.py`, `src/ai/`, and `src/manager/` can align without
touching files outside `src/tui/`.

---

## New public methods

### `log_cost(tokens_in: int, tokens_out: int, model: str) -> None`

Call this after every LLM response to keep the running cost counter up to date.

| Parameter   | Type  | Description                                                          |
|-------------|-------|----------------------------------------------------------------------|
| `tokens_in` | `int` | Input (prompt) token count for **this single call** (not cumulative)|
| `tokens_out`| `int` | Output (completion) token count for **this single call**            |
| `model`     | `str` | Model identifier exactly as returned by the API                     |

**Recognised model strings** (others default to $0 cost, counts still shown):

- `"claude-sonnet-4-6"`
- `"claude-haiku-4-5-20251001"`

**Example call (from executor / main loop):**

```python
display.log_cost(
    tokens_in=response.usage.input_tokens,
    tokens_out=response.usage.output_tokens,
    model=response.model,
)
```

The display accumulates totals across the session. The formatted cost string
appears pinned at the bottom of the left panel, e.g.:

```
Cost: $0.0042  (↑ 1,234 / ↓ 567 tokens)
```

---

### `log_step(step_num: int, total: int, label: str) -> None`

Call this whenever the agent transitions to a new execution step.

| Parameter  | Type  | Description                                           |
|------------|-------|-------------------------------------------------------|
| `step_num` | `int` | Current step index, **1-based**                       |
| `total`    | `int` | Total steps planned for this run                      |
| `label`    | `str` | Short action name, e.g. `"create_post"`, `"login"`   |

The indicator appears just below the status badge in the left panel:

```
  [Executing]  step 2/3: create_post
```

**Example call:**

```python
for i, action in enumerate(plan.actions, start=1):
    display.log_step(i, len(plan.actions), action.name)
    display.set_status("Executing")
    await executor.run(action)
```

---

## Behaviour change in `log_intercept()`

`log_intercept()` now applies secret redaction to the `url` argument **before
storing or displaying it**.  Callers will see `[REDACTED]` in place of:

- Bearer token values in query strings or path segments  
  (`Bearer <token>` → `Bearer [REDACTED]`)
- Cookie-style key=value pairs where the value is longer than 16 alphanumeric
  characters (`session=<long-value>` → `session=[REDACTED]`)

**Callers should not pre-strip URLs** — the TUI handles this automatically.
Raw URLs with real tokens must never be passed to `log_thought()` either, as
the same redaction is applied there.

---

## Plain-text / CI fallback

When the environment sets `NO_COLOR` (any value) or `TERM=dumb`, the Rich TUI
is replaced with plain `print()` output.  All methods still work; output lines
are prefixed with `[STATUS]`, `[COST]`, `[STEP]`, `[NETWORK]` for easy
grepping in CI logs.

---

## Existing interface (unchanged — do not modify callers)

```python
display.set_status(status: str)          # "Idle" | "Planning" | "Executing" | ...
display.log_thought(thought: str)        # append a timestamped line to the stream
display.log_intercept(url, status,       # record one captured HTTP exchange
                      raw_bytes,
                      compact_bytes)
with AgentDisplay() as display: ...      # context manager — starts/stops Live TUI
```
