# Cross-boundary requests from `src/ai/`

These are interface needs that require changes in other modules. Owners of those
modules should implement the stubs described below before the next integration pass.

---

## Request to: `tui`

### 1. `AgentDisplay.log_cost(tokens_in: int, tokens_out: int, model: str) -> None`

Called after every LLM response so the TUI can display a running token/cost
summary.  Anthropic responses expose usage via `response.usage.input_tokens`
and `response.usage.output_tokens`.

**Signature (async or sync — either is fine, the callers will await if needed):**
```python
def log_cost(self, tokens_in: int, tokens_out: int, model: str) -> None: ...
```

**Called from:** `src/ai/discovery/planner.py` and `src/ai/decision/executor.py`
after every `client.messages.create()` call.

---

### 2. `AgentDisplay.log_step(step_num: int, total: int, label: str) -> None`

Called at the start of each step inside `ExecutionAgent.execute_plan()` so the
TUI can show progress through a multi-step plan (e.g. "Step 2/4: fetch_user").

**Signature:**
```python
def log_step(self, step_num: int, total: int, label: str) -> None: ...
```

**Called from:** `src/ai/decision/executor.py`, inside `execute_plan()`, once per
step before `self.execute()` is awaited.

---

## Request to: `security`

### 3. `sanitize_for_llm(text: str) -> str`

`src/serialization/models.py` currently ships a minimal implementation that:
- Strips null bytes (`\x00`)
- Truncates to 4 000 characters (appending `[truncated]` if cut)

The security agent should review and, if appropriate, replace or extend this with
a version that also handles:
- Other C0/C1 control characters (e.g. `\x01`–`\x1f`, `\x80`–`\x9f`)
- Unicode direction-override characters (e.g. U+202E)
- Prompt-injection patterns (e.g. `<|endoftext|>`, role-boundary markers)

If the security agent provides a hardened `sanitize_for_llm`, the `sanitize()`
function in `src/serialization/models.py` should be updated to delegate to it.
The public function name and signature must remain:

```python
def sanitize(text: str) -> str: ...
```
