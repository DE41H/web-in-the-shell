"""
Web in the Shell — network-level AI agent.

Modes:
  default (no --mock): live LLM; interactive setup runs when needed.
  --mock:              full pipeline with no API key, hardcoded 2-step plan.
  --no-interactive --intent TEXT: single-shot CI/script run; no prompts.
  --memory ...:        conversation-memory subcommand; runs and exits.
"""

import argparse
import asyncio
import getpass
import json
import os
import re
import uuid
from dataclasses import dataclass
from datetime import datetime, UTC
from typing import Any
from urllib.parse import urlparse

import httpx
from rich.console import Console
from rich.panel import Panel
from rich.prompt import Confirm, IntPrompt
from rich.table import Table
from rich.text import Text

from network.security.stealth import StealthBrowser
from network.intercept.consent import detect_captcha, dismiss_consent, dismiss_overlays
from network.intercept.sniffer import PacketSniffer, CapturedResponse
from network.session.manager import SessionManager
from network.dispatch.client import DispatchClient
from serialization.models import compact_from_capture, CompactStateModel
from security.sanitize import sanitize_for_llm
from security.allowlist import validate_url
from ai.provider import (
    LLMClient,
    DEFAULT_MODELS,
    DEFAULT_RECOVERY_MODELS,
    PROVIDER_ENV_VARS,
    fetch_available_models,
)
from ai.discovery.planner import PlannerAgent, Plan
from ai.decision.answer import AnswerAgent
from ai.decision.executor import ExecutionAgent, ExecutionResult
from ai.errors import classify
from persistence import (
    ConvoStore,
    DEFAULT_DB_PATH,
    Convo,
    ConvoMessage,
    FormFieldStore,
    SessionStore,
    init_db,
)
from tui.display import AgentDisplay

_console = Console()


async def _probe_url(url: str) -> str | None:
    """Probe a single URL; return the final URL after redirects, or None if unreachable.

    Tries HEAD first (no body transfer), falls back to GET only on 405. One retry
    on transient failures. Used in the nav-retry loop where a shared client is not
    available; for batch probing use _pick_reachable_domain instead.
    """
    p = urlparse(url)
    scheme = p.scheme or "https"
    host = p.hostname
    if not host:
        return None
    base = f"{scheme}://{host}/"
    for _ in range(2):
        try:
            async with httpx.AsyncClient(timeout=3.0, follow_redirects=True) as c:
                r = await c.head(base)
                if r.status_code == 405:
                    r = await c.get(base)
                if r.status_code >= 100:
                    return str(r.url).rstrip("/") or base.rstrip("/")
        except Exception:
            continue
    return None


async def _pick_reachable_domain(urls: list[str]) -> str | None:
    """Probe candidate domains concurrently; return the final URL of the best reachable one.

    Strategy:
    - Deduplicates candidates and automatically expands each with its www/non-www
      counterpart (e.g. github.com → www.github.com and vice-versa).
    - All probes share one httpx.AsyncClient (single TLS pool, no per-call handshake).
    - Uses HEAD requests to avoid downloading page bodies.
    - Uses asyncio.wait(FIRST_COMPLETED): returns as soon as the highest-priority
      reachable domain is determined, and cancels remaining probes immediately.
    - Ordering is preserved: the primary URL (index 0) wins over any candidate.
    """
    if not urls:
        return None

    # Build a deduplicated, priority-ordered list expanded with www/non-www variants.
    seen: set[str] = set()
    ranked: list[tuple[int, str]] = []

    def _enqueue(priority: int, u: str) -> None:
        key = u.rstrip("/").lower()
        if key not in seen:
            seen.add(key)
            ranked.append((priority, u))

    for i, u in enumerate(urls):
        _enqueue(2 * i, u)
        p = urlparse(u)
        host = (p.netloc or p.path).lower()
        alt = (
            f"{p.scheme}://{host[4:]}"       # strip www.
            if host.startswith("www.")
            else f"{p.scheme}://www.{host}"  # add www.
        )
        _enqueue(2 * i + 1, alt)

    ranked.sort(key=lambda x: x[0])
    probe_urls = [u for _, u in ranked]
    n = len(probe_urls)

    async def _probe_one(u: str, client: httpx.AsyncClient) -> str | None:
        p2 = urlparse(u)
        base = f"{p2.scheme or 'https'}://{p2.hostname}/"
        try:
            r = await client.head(base)
            if r.status_code == 405:
                r = await client.get(base)
            if r.status_code >= 100:
                return str(r.url).rstrip("/") or base.rstrip("/")
        except Exception:
            pass
        return None

    timeout = httpx.Timeout(connect=2.0, read=4.0, write=2.0, pool=1.0)
    limits = httpx.Limits(max_connections=n, max_keepalive_connections=0)
    async with httpx.AsyncClient(
        timeout=timeout, follow_redirects=True, limits=limits
    ) as client:
        task_to_idx: dict[asyncio.Task, int] = {
            asyncio.create_task(_probe_one(probe_urls[i], client)): i
            for i in range(n)
        }
        results: dict[int, str] = {}
        pending = set(task_to_idx)
        try:
            while pending:
                done, pending = await asyncio.wait(
                    pending, return_when=asyncio.FIRST_COMPLETED
                )
                for t in done:
                    val = t.result()
                    if val is not None:
                        results[task_to_idx[t]] = val
                if results:
                    best = min(results)
                    # Return as soon as no pending task can beat our best result.
                    if not any(task_to_idx[t] < best for t in pending):
                        return results[best]
        finally:
            for t in pending:
                t.cancel()
            if pending:
                await asyncio.gather(*pending, return_exceptions=True)

    return results[min(results)] if results else None


def _join_url(base: str, endpoint: str | None) -> str:
    """Join a base URL and an endpoint ensuring a single separating slash.

    Examples:
      _join_url('https://x.com', '/a') -> 'https://x.com/a'
      _join_url('https://x.com/', 'a') -> 'https://x.com/a'
      _join_url('https://x.com', None) -> 'https://x.com'
    """
    if not endpoint:
        return base.rstrip("/")
    return base.rstrip("/") + "/" + endpoint.lstrip("/")


# ---------------------------------------------------------------------------
# Error handling
# ---------------------------------------------------------------------------

def _handle_api_error(
    exc: Exception,
    display: "AgentDisplay",
    *,
    provider: str = "",
) -> None:
    """Classify *exc* and surface it through the display.

    All branching lives in :func:`ai.errors.classify`; this wrapper just
    maps the resulting ``ErrorInfo`` onto TUI state (status badge + pinned
    error line + thought stream). Provider-specific billing hints are baked
    into the classifier when ``provider`` is supplied.
    """
    info = classify(exc, provider=provider, source="llm")
    display.log_error(info)
    display.set_status("Failed")


# ---------------------------------------------------------------------------
# Session config
# ---------------------------------------------------------------------------

@dataclass
class SessionConfig:
    mock: bool = False
    provider: str = "anthropic"
    api_key: str = ""
    model: str | None = None
    recovery_model: str | None = None
    target: str = ""        # empty → AI-derived from plan.target_domain
    login: bool = False
    replan: int = 2
    no_interactive: bool = False


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------

def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="web-in-the-shell",
        description=(
            "Network-level AI agent. Type your goal at the prompt — "
            "the AI determines the target site and dispatches HTTP actions. "
            "Use /commands inside the session to adjust settings."
        ),
    )
    parser.add_argument("--mock",           action="store_true",
                        help="Mock mode: hardcoded plan, no LLM, no API key required.")
    parser.add_argument("--intent",         metavar="TEXT", default=None,
                        help="Goal for single-shot non-interactive run "
                             "(requires --no-interactive).")
    parser.add_argument("--provider",       metavar="NAME", default=None,
                        choices=list(DEFAULT_MODELS),
                        help="LLM provider: " + ", ".join(DEFAULT_MODELS) + ".")
    parser.add_argument("--api-key",        metavar="KEY",  default=None,
                        help="LLM API key. Falls back to the provider env var.")
    parser.add_argument("--model",          metavar="NAME", default=None,
                        help="Override primary model (planner + executor).")
    parser.add_argument("--recovery-model", metavar="NAME", default=None,
                        help="Override recovery model (fast hot-path).")
    parser.add_argument("--replan",         metavar="N", type=int, default=None,
                        help="Max replan attempts on execution failure (default 2).")
    parser.add_argument("--login",          action="store_true",
                        help="Open a visible browser window for a manual login handshake.")
    parser.add_argument("--no-interactive", action="store_true",
                        help="Skip every prompt; requires --intent (and --api-key or env var).")
    parser.add_argument(
        "--memory", nargs="+", metavar=("CMD", "ARG"), default=None,
        help="Memory subcommand: list | clear INTENT | clear-all.",
    )
    return parser


def _apply_args_to_config(args: argparse.Namespace, config: SessionConfig) -> None:
    if args.provider is not None:
        config.provider = args.provider
    if args.api_key is not None:
        config.api_key = args.api_key
    if args.model is not None:
        config.model = args.model
    if args.recovery_model is not None:
        config.recovery_model = args.recovery_model
    if args.replan is not None:
        config.replan = args.replan
    if args.login:
        config.login = True
    if args.mock:
        config.mock = True


def _fill_api_key_from_env(config: SessionConfig) -> None:
    if config.provider == "ollama":
        if not config.api_key:
            config.api_key = "ollama"
        return
    if config.api_key:
        return
    env_var = PROVIDER_ENV_VARS.get(config.provider, "")
    if env_var and os.environ.get(env_var):
        config.api_key = os.environ[env_var]


# ---------------------------------------------------------------------------
# Ollama health check
# ---------------------------------------------------------------------------

async def _check_ollama() -> bool:
    try:
        async with httpx.AsyncClient(timeout=3.0) as c:
            r = await c.get("http://localhost:11434/")
            return r.status_code < 500
    except Exception:
        return False


# ---------------------------------------------------------------------------
# Interactive setup (async — fetches model list from provider)
# ---------------------------------------------------------------------------

async def _interactive_setup(args: argparse.Namespace) -> SessionConfig:
    """One-time session setup: provider, API key, model. Target/intent go to REPL."""
    config = SessionConfig()
    _apply_args_to_config(args, config)

    _console.print(Panel(
        Text("WEB IN THE SHELL", style="bold cyan", justify="center"),
        border_style="cyan",
        subtitle="[dim]type your goal at the prompt, /help for commands[/dim]",
    ))

    # ── Provider selection ───────────────────────────────────────────────
    if args.provider is None:
        providers = list(DEFAULT_MODELS)
        _console.print("\n  [bold]Select LLM provider:[/bold]")
        for idx, prov in enumerate(providers, 1):
            suffix = "  [dim](local — no key needed)[/dim]" if prov == "ollama" else ""
            _console.print(
                f"  [cyan]{idx}[/cyan]. {prov}  [dim]{DEFAULT_MODELS[prov]}[/dim]{suffix}"
            )
        choice = IntPrompt.ask("  Choice", default=1)
        config.provider = providers[max(1, min(choice, len(providers))) - 1]

    # ── Ollama health check ──────────────────────────────────────────────
    if config.provider == "ollama" and not await _check_ollama():
        _console.print(
            "\n  [bold red]Ollama server not found at localhost:11434.[/bold red]\n"
            "  Start it with: [cyan]ollama serve[/cyan]\n"
            "  Switching provider — re-select:\n"
        )
        providers = list(DEFAULT_MODELS)
        for idx, prov in enumerate(providers, 1):
            _console.print(f"  [cyan]{idx}[/cyan]. {prov}")
        choice = IntPrompt.ask("  Choice", default=1)
        config.provider = providers[max(1, min(choice, len(providers))) - 1]

    # ── API key ──────────────────────────────────────────────────────────
    if not config.api_key:
        if config.provider == "ollama":
            config.api_key = "ollama"
        else:
            env_var = PROVIDER_ENV_VARS.get(config.provider, "")
            if env_var and os.environ.get(env_var):
                _console.print(f"\n  [green]✓ {env_var} found in environment[/green]")
                config.api_key = os.environ[env_var]
            else:
                _console.print()
                try:
                    key = getpass.getpass(f"  Enter {config.provider} API key: ").strip()
                except KeyboardInterrupt:
                    raise SystemExit("\nAborted.")
                if not key:
                    env_hint = PROVIDER_ENV_VARS.get(config.provider, "the matching env var")
                    raise SystemExit(
                        f"No API key provided for {config.provider}. "
                        f"Set {env_hint}, pass --api-key, or use --mock."
                    )
                config.api_key = key

    # ── Model selection (dynamic fetch) ─────────────────────────────────
    if config.model is None:
        with _console.status("  Fetching available models…"):
            models = await fetch_available_models(config.provider, config.api_key)

        default_model = DEFAULT_MODELS.get(config.provider, "")
        if len(models) <= 1:
            config.model = models[0] if models else default_model
            _console.print(f"\n  Model: [cyan]{config.model}[/cyan]")
        else:
            _console.print(f"\n  [bold]Available models ({config.provider}):[/bold]")
            default_idx = next(
                (i for i, m in enumerate(models, 1) if m == default_model), 1
            )
            for i, m in enumerate(models[:20], 1):
                marker = "  [dim](recommended)[/dim]" if m == default_model else ""
                _console.print(f"  [cyan]{i}[/cyan]. {m}{marker}")
            if len(models) > 20:
                _console.print(f"  [dim]... and {len(models) - 20} more[/dim]")
            choice = IntPrompt.ask("  Model", default=default_idx)
            config.model = models[min(max(choice, 1), len(models)) - 1]

    _console.print()
    return config


async def _build_config_async(
    args: argparse.Namespace,
    parser: argparse.ArgumentParser,
) -> SessionConfig:
    if args.mock:
        if args.no_interactive and not args.intent:
            parser.error("--mock --no-interactive requires --intent TEXT")
        config = SessionConfig(mock=True, no_interactive=args.no_interactive)
        _apply_args_to_config(args, config)
        return config

    if args.no_interactive:
        if not args.intent:
            parser.error("--no-interactive requires --intent TEXT")
        config = SessionConfig(no_interactive=True)
        _apply_args_to_config(args, config)
        _fill_api_key_from_env(config)
        if config.provider != "ollama" and not config.api_key:
            env_hint = PROVIDER_ENV_VARS.get(config.provider, "the matching env var")
            parser.error(
                f"--no-interactive: no API key for {config.provider}. "
                f"Pass --api-key or set {env_hint}."
            )
        if config.model is None:
            config.model = DEFAULT_MODELS.get(config.provider)
        return config

    return await _interactive_setup(args)


# ---------------------------------------------------------------------------
# Mock plan
# ---------------------------------------------------------------------------

_MOCK_PLAN = Plan(
    target_domain="https://jsonplaceholder.typicode.com",
    target_endpoints=["/posts"],
    action="create_post",
    parameters={"title": "Agent Test", "body": "Dispatched by Web in the Shell", "userId": 1},
    steps=[
        {"action": "fetch_posts", "endpoint": "/posts", "parameters": {}, "method": "GET"},
        {
            "action": "create_post",
            "endpoint": "/posts",
            "parameters": {
                "title": "Agent Test",
                "body": "Dispatched by Web in the Shell",
                "userId": 1,
            },
            "method": "POST",
        },
    ],
)


# ---------------------------------------------------------------------------
# Form detection + auto-fill
# ---------------------------------------------------------------------------

_SENSITIVE_TYPES: frozenset[str] = frozenset({"password"})
_SENSITIVE_NAME_RE = re.compile(
    r"password|passwd|pwd|cvv|ssn|pin|otp|secret|card.?num", re.IGNORECASE
)

_FORM_DETECT_JS = """
() => {
  const inputs = Array.from(document.querySelectorAll(
    'input:not([type="hidden"]):not([type="submit"]):not([type="button"])' +
    ':not([type="reset"]):not([type="checkbox"]):not([type="radio"]), textarea, select'
  ));
  return inputs.map(el => {
    const lbl = el.id ? document.querySelector('label[for="' + el.id + '"]') : null;
    const nm = el.name || el.id || '';
    return {
      name:     nm,
      type:     el.type || 'text',
      label:    lbl ? lbl.textContent.trim() : (el.placeholder || nm),
      required: el.required,
      id:       el.id || '',
    };
  }).filter(f => f.name);
}
"""


def _is_sensitive(field_type: str, field_name: str) -> bool:
    return field_type in _SENSITIVE_TYPES or bool(_SENSITIVE_NAME_RE.search(field_name))


async def _detect_forms(page: Any) -> list[dict[str, Any]]:
    """Extract visible input fields from the current page via JS evaluation."""
    try:
        raw: list[dict[str, Any]] = await page.evaluate(_FORM_DETECT_JS)
    except Exception:
        return []
    result = []
    for f in raw:
        if f.get("id"):
            f["selector"] = f'#{f["id"]}'
        elif f.get("name"):
            escaped = f["name"].replace('"', '\\"')
            f["selector"] = f'[name="{escaped}"]'
        else:
            continue
        result.append(f)
    return result


async def _find_submit(page: Any) -> Any | None:
    """Return the most prominent visible submit control on the page, or None."""
    # Prefer explicit type=submit first (most reliable signal)
    for sel in ('button[type="submit"]', 'input[type="submit"]', '[type="submit"]'):
        try:
            el = await page.query_selector(sel)
            if el and await el.is_visible():
                return el
        except Exception:
            continue
    # Fall back to buttons whose text matches common submit labels
    _SUBMIT_TEXTS = [
        "Sign in", "Log in", "Login", "Submit", "Continue",
        "Next", "Send", "Go", "Confirm", "Proceed",
    ]
    for text in _SUBMIT_TEXTS:
        try:
            # query_selector with :text() is a Playwright extension
            el = await page.query_selector(f"button:has-text('{text}')")
            if el and await el.is_visible():
                return el
        except Exception:
            continue
    return None


async def _submit_form(page: Any, display: "AgentDisplay") -> bool:
    """Click the page's submit button, or press Enter if none is found.

    Returns True if a submission attempt was made.
    """
    submit_el = await _find_submit(page)
    try:
        if submit_el:
            display.log_thought("Submitting form…")
            await submit_el.click()
            return True
        # No explicit submit button — press Enter on the active element
        display.log_thought("No submit button found — pressing Enter…")
        await page.keyboard.press("Enter")
        return True
    except Exception:
        return False


async def _prompt_and_fill_forms(
    page: Any,
    domain: str,
    form_store: FormFieldStore,
    display: "AgentDisplay",
    *,
    no_interactive: bool = False,
) -> bool:
    """Detect form fields, fill them, and submit the form.

    Fields that have a saved value in FormFieldStore are filled automatically.
    Sensitive fields (password, OTP, CVV) are always collected via a masked
    terminal prompt unless no_interactive=True, in which case they are skipped.

    If every field was satisfied without user input (fully from store), the form
    is submitted automatically. If user input was needed, the form is still
    submitted after the prompts complete — the user just had to provide some values.

    Returns True if a form submit was attempted.
    """
    fields = await _detect_forms(page)
    if not fields:
        return False

    saved = await form_store.get_all_for_domain(domain)
    to_fill: list[tuple[dict, str]] = []

    # ── Determine which fields need prompting ────────────────────────────────
    sensitive_fields = [f for f in fields if _is_sensitive(f["type"], f["name"])]
    has_sensitive = bool(sensitive_fields)

    # If all non-sensitive fields are in the store AND there are no sensitive
    # fields, we can fill and submit entirely without user interaction.
    if not has_sensitive:
        store_vals = [(f, saved[f["name"]]) for f in fields if saved.get(f["name"])]
        if store_vals:
            display.log_thought(
                f"Auto-filling {len(store_vals)} field(s) from store (no user input needed)…"
            )
            for f, val in store_vals:
                try:
                    await page.fill(f["selector"], val)
                except Exception:
                    pass
            return await _submit_form(page, display)
        # Nothing in store and no sensitive fields — nothing to fill
        return False

    # ── Interactive path (sensitive fields present or no_interactive skip) ───
    if no_interactive:
        # In scripted mode we cannot prompt; fill only from store
        store_vals = [(f, saved[f["name"]]) for f in fields if saved.get(f["name"])]
        if store_vals:
            display.log_thought(
                f"Non-interactive: auto-filling {len(store_vals)} field(s) from store…"
            )
            for f, val in store_vals:
                try:
                    await page.fill(f["selector"], val)
                except Exception:
                    pass
            return await _submit_form(page, display)
        return False

    # Pause Rich Live so the terminal is readable during prompts
    paused = False
    if display._live and not display._plain:
        display._live.stop()
        paused = True

    try:
        _console.print("\n  [bold]Form fields detected:[/bold]")
        for f in fields:
            sensitive = _is_sensitive(f["type"], f["name"])
            saved_val = saved.get(f["name"])
            label = f.get("label") or f["name"]

            if sensitive:
                try:
                    val = getpass.getpass(f"    {label} (sensitive, not stored): ").strip()
                except (EOFError, KeyboardInterrupt):
                    val = ""
            elif saved_val:
                _console.print(
                    f"    [dim]{label}[/dim]: [green]{saved_val[:40]}[/green]"
                    "  [dim](auto-fill — press Enter to keep)[/dim]"
                )
                try:
                    override = _console.input("    ").strip()
                except (EOFError, KeyboardInterrupt):
                    override = ""
                val = override if override else saved_val
                if override:
                    await form_store.save(domain, f["name"], f["type"], override)
            else:
                continue

            if val:
                to_fill.append((f, val))

        _console.print()
    finally:
        if paused and display._live:
            display._live.start()

    if not to_fill:
        return False

    for f, val in to_fill:
        try:
            await page.fill(f["selector"], val)
        except Exception:
            pass

    return await _submit_form(page, display)


# ---------------------------------------------------------------------------
# Pipeline
# ---------------------------------------------------------------------------

def _build_replan_context(executor: ExecutionAgent, results: list[ExecutionResult]) -> str:
    failed = [r for r in results if not r.success]
    parts = ["Previous attempt failed:"]
    parts += [f"  {r.endpoint} HTTP {r.status_code}: {r.error}" for r in failed]
    if executor.state_history:
        # Send only the most recent state to keep context small for smaller models.
        parts += ["", "Last state:", executor.state_history[-1].to_llm_context()]
    return "\n".join(parts)


async def _run(config: SessionConfig, display: AgentDisplay, intent: str) -> None:
    safe_intent = sanitize_for_llm(intent)
    display.set_status("Planning")
    display.log_thought(f"Goal: {safe_intent[:72]}…")

    await init_db(DEFAULT_DB_PATH)

    main_client: LLMClient | None = None
    recovery_client: LLMClient | None = None
    if not config.mock:
        main_model     = config.model or DEFAULT_MODELS.get(config.provider)
        recovery_model = config.recovery_model or DEFAULT_RECOVERY_MODELS.get(config.provider)
        main_client     = LLMClient(config.provider, config.api_key, main_model)
        recovery_client = LLMClient(config.provider, config.api_key, recovery_model)
        display.log_thought(f"Provider: {config.provider} | Model: {main_client.model}")

    async with (
        ConvoStore(DEFAULT_DB_PATH) as convos,
        FormFieldStore(DEFAULT_DB_PATH) as form_store,
        SessionStore(DEFAULT_DB_PATH) as session_store,
    ):
        last_planner: PlannerAgent | None = None

        # ── Step 1: Plan first — AI determines target ─────────────────────
        if config.mock:
            plan = _MOCK_PLAN
            display.log_thought("Mock mode — using hardcoded 2-step plan.")
        else:
            planner = PlannerAgent(main_client, convos=convos)
            try:
                plan = await planner.plan(safe_intent)
            except ValueError as exc:
                if str(exc) == 'Planner produced no actionable tool call':
                    plan = await planner.handle_fallback(
                        safe_intent,
                        messages=planner.last_messages,
                    )
                else:
                    display.set_status("Failed")
                    display.log_thought(f"Planning failed: {exc}")
                    return
            except Exception as exc:
                display.set_status("Failed")
                display.log_thought(f"Planning failed: {exc}")
                return
            last_planner = planner
            if planner.last_usage:
                u = planner.last_usage
                display.log_cost(u["input"], u["output"], u["model"])
            display.log_thought(f"Plan → {plan.target_domain}  steps={len(plan.steps)}")

        target = config.target or plan.target_domain

        # Probe primary + candidate domains concurrently to verify reachability
        # and follow any redirect chain (http→https, bare→www, etc.) before
        # committing to navigation. Skip when the user supplied --target explicitly.
        if not config.target and not config.mock:
            probe_candidates = [plan.target_domain] + [
                d if d.startswith(("http://", "https://")) else "https://" + d
                for d in plan.candidate_domains
            ]
            display.log_thought(
                f"Probing {len(probe_candidates)} candidate domain(s)…"
            )
            probed = await _pick_reachable_domain(probe_candidates)
            if probed:
                # Strip trailing slash and any path from the probed URL so we
                # keep only scheme+host as the base target.
                p = urlparse(probed)
                target = f"{p.scheme}://{p.netloc}"
                if target != plan.target_domain:
                    display.log_thought(
                        f"Domain resolved: {plan.target_domain} → {target}"
                    )
                else:
                    display.log_thought(f"Domain reachable: {target}")
            else:
                display.log_thought(
                    f"All domain probes failed ({', '.join(probe_candidates)}) "
                    "— proceeding anyway."
                )

        display.log_thought(f"Target: {target}")

        # Derive intercept patterns from planned endpoints
        if plan.target_endpoints:
            patterns = [re.escape(ep.split("?")[0]) for ep in plan.target_endpoints]
        else:
            patterns = [r"/"]
        nav_url = target
        if not nav_url.startswith(("http://", "https://")):
            nav_url = "https://" + nav_url

        # ── Step 2: Browser + interception ──────────────────────────────
        display.set_status("Intercepting")
        display.log_thought("Launching stealth Chromium…")

        async with StealthBrowser(headless=True) as browser:
            if config.login:
                display.log_thought("Login handshake — opening visible browser…")
                page = await browser.login_handshake(target)
            else:
                page = await browser.new_page()

            session = SessionManager()
            session.attach(page)

            sniffer = PacketSniffer(patterns)
            sniffer.attach(page)

            intercepts: list[tuple[CapturedResponse, CompactStateModel]] = []

            async def _collect_stream() -> None:
                async for cap in sniffer.stream():
                    state = compact_from_capture(cap)
                    intercepts.append((cap, state))
                    display.log_intercept(cap.url, cap.status, cap.raw_size, state.compact_size)
                    pct = int((1 - state.compact_size / max(cap.raw_size, 1)) * 100)
                    display.log_thought(
                        f"↙ {cap.url[-48:]}  {cap.raw_size:,}b→{state.compact_size}b  ({pct}%↓)"
                    )

            host = urlparse(nav_url).hostname or ""
            display.log_thought(f"Navigating → {nav_url}")

            # Attempt navigation with simple replan-on-failure loop.
            # We allow up to (config.replan + 1) navigation attempts when
            # not in mock mode; mock mode does a single attempt.
            stream_task = asyncio.create_task(_collect_stream())
            nav_ok = False
            nav_attempt = 0
            max_nav_attempts = (config.replan + 1) if not config.mock else 1
            try:
                while nav_attempt < max_nav_attempts:
                    # Ensure the URL has a scheme and is valid for our validator
                    if not nav_url.startswith(("http://", "https://")):
                        nav_url = "https://" + nav_url

                    try:
                        validate_url(nav_url)
                    except ValueError as exc:
                        # Validation failed (bad URL) — surface and try to replan
                        info = classify(exc=exc, source="browser")
                        display.log_error(info)
                        display.set_status("Failed")
                        if config.mock or nav_attempt + 1 >= max_nav_attempts or not main_client:
                            break
                        display.log_thought("URL validation failed — requesting new plan…")
                        planner = PlannerAgent(main_client, convos=convos)
                        plan = await planner.plan(
                            safe_intent,
                            context=f"Navigation to {nav_url} failed. {info.title}: {info.detail}"
                        )
                        last_planner = planner
                        target = config.target or plan.target_domain
                        nav_url = target

                        # Quick reachability probe before attempting browser navigation.
                        try:
                            reachable = await _probe_url(nav_url)
                        except Exception:
                            reachable = False
                        if not reachable:
                            display.log_thought(f"Probe failed for {nav_url} — trying a new plan.")
                            nav_attempt += 1
                            continue
                        nav_attempt += 1
                        continue

                    try:
                        # Phase 1: load DOM only — networkidle can block on CAPTCHA / SSE.
                        await page.goto(nav_url, wait_until="domcontentloaded", timeout=30_000)
                        await session.sync_cookies(page)
                        await session.restore(host, session_store)

                        # Phase 2: autonomous banner/overlay/captcha handling.
                        await dismiss_consent(page)
                        await dismiss_overlays(page)
                        captcha_name = await detect_captcha(page)
                        if captcha_name:
                            display.log_thought(
                                f"CAPTCHA detected ({captcha_name}) — "
                                "rerun with --login for manual handshake."
                            )

                        # Phase 3: graceful networkidle (best-effort; 8 s cap).
                        try:
                            await page.wait_for_load_state("networkidle", timeout=8_000)
                        except Exception:
                            pass

                        # Phase 4: form detection + fill *before* stream_task cancels
                        # so that any auth/submit XHR is captured by the sniffer.
                        if not config.mock:
                            submitted = await _prompt_and_fill_forms(
                                page,
                                target,
                                form_store,
                                display,
                                no_interactive=config.no_interactive,
                            )
                            if submitted:
                                await session.sync_cookies(page)
                                await dismiss_consent(page)
                                await dismiss_overlays(page)
                                try:
                                    await page.wait_for_load_state("networkidle", timeout=8_000)
                                except Exception:
                                    pass

                        nav_ok = True
                        break
                    except Exception as exc:
                        # Navigation failed — classify and optionally replan
                        info = classify(exc=exc, source="browser")
                        display.log_error(info)
                        display.set_status("Failed")
                        display.log_thought(f"Cannot reach {nav_url}: {exc}")
                        display.log_thought("Check your internet connection and the target URL.")
                        if config.mock or nav_attempt + 1 >= max_nav_attempts or not main_client:
                            break
                        display.log_thought(
                            f"Replanning due to navigation failure "
                            f"(attempt {nav_attempt + 1}/{config.replan})…"
                        )
                        planner = PlannerAgent(main_client, convos=convos)
                        plan = await planner.plan(
                            safe_intent, context=f"Navigation failed for {nav_url}: {exc}"
                        )
                        last_planner = planner
                        target = config.target or plan.target_domain
                        nav_url = target
                    finally:
                        nav_attempt += 1
            finally:
                stream_task.cancel()
                try:
                    await stream_task
                except asyncio.CancelledError:
                    pass

            if not nav_ok:
                return

            for cap in sniffer.drain():
                state = compact_from_capture(cap)
                intercepts.append((cap, state))
                display.log_intercept(cap.url, cap.status, cap.raw_size, state.compact_size)

            display.log_thought(f"Intercepted {len(intercepts)} response(s).")
            primary_state = intercepts[0][1] if intercepts else None

            # ── Step 3: Execution (with optional replan loop) ────────────
            async with DispatchClient(session, base_url=target) as dispatch:
                if config.mock:
                    executor = None
                    answer_agent = None
                else:
                    assert main_client is not None
                    assert recovery_client is not None
                    executor = ExecutionAgent(dispatch, main_client, recovery_client)
                    answer_agent = AnswerAgent(recovery_client)
                replan_ctx      = ""
                overall_success = False
                results: list[ExecutionResult] = []

                for attempt in range(config.replan + 1):
                    if attempt > 0:
                        display.set_status(f"Recovering ({attempt}/{config.replan})")
                        display.log_thought(f"Replanning (attempt {attempt}/{config.replan})…")
                        if not config.mock:
                            planner = PlannerAgent(main_client, convos=convos)
                            # 403/404/410 = wrong endpoint or missing auth;
                            # status 0 = network-level failure (connection refused,
                            # ReadError, unreachable host) — the chosen domain is wrong.
                            # In all these cases replanning with the same intent produces
                            # the same bad result.  Use handle_fallback (DuckDuckGo search)
                            # so the model gets real API docs before choosing a domain/endpoint.
                            _needs_discovery = (
                                results
                                and any(not r.success for r in results)
                                and all(
                                    r.status_code in (0, 403, 404, 410)
                                    for r in results
                                    if not r.success
                                )
                            )
                            try:
                                if _needs_discovery:
                                    display.log_thought(
                                        "Running API discovery search to find correct endpoint…"
                                    )
                                    plan = await planner.handle_fallback(
                                        safe_intent, messages=[]
                                    )
                                else:
                                    plan = await planner.plan(
                                        safe_intent, context=replan_ctx
                                    )
                            except Exception as exc:
                                display.log_thought(f"Replan failed: {exc}")
                                break
                            last_planner = planner

                    display.set_status("Executing")
                    steps = plan.steps or [
                        {
                            "action":     plan.action,
                            "endpoint":   (
                                plan.target_endpoints[0]
                                if plan.target_endpoints else "/"
                            ),
                            "parameters": plan.parameters,
                            "method":     "POST",
                        }
                    ]
                    total = len(steps)

                    if config.mock:
                        results = []
                        for i, step in enumerate(steps, 1):
                            display.log_step(i, total, step["action"])
                            display.log_thought(f"→ {step['action']}  {step['endpoint']}")
                            method = step.get("method", "POST").upper()
                            if method == "GET" or not step["parameters"]:
                                http_resp = await dispatch.get(step["endpoint"])
                            else:
                                http_resp = await dispatch.post(
                                    step["endpoint"], step["parameters"]
                                )
                            r = ExecutionResult(
                                success=http_resp.is_success,
                                endpoint=step["endpoint"],
                                status_code=http_resp.status_code,
                                response_body=http_resp.json() if http_resp.is_success else None,
                                error=None if http_resp.is_success else http_resp.text[:200],
                            )
                            results.append(r)
                            if http_resp.is_success:
                                display.log_thought(
                                    f"  ✓ {http_resp.status_code}  "
                                    f"{json.dumps(http_resp.json())[:120]}"
                                )
                            else:
                                display.log_thought(
                                    f"  ✗ {http_resp.status_code}  {http_resp.text[:120]}"
                                )
                                break
                    else:
                        assert executor is not None
                        results = await executor.execute_plan(plan, primary_state)
                        if executor.last_usage:
                            u = executor.last_usage
                            display.log_cost(u["input"], u["output"], u["model"])
                        for i, result in enumerate(results, 1):
                            action = steps[i - 1]["action"] if i <= len(steps) else "step"
                            display.log_step(i, total, action)
                            if result.success:
                                display.log_thought(
                                    f"  ✓ {result.status_code}  "
                                    f"{json.dumps(result.response_body)[:120]}"
                                )
                            else:
                                display.log_thought(
                                    f"  ✗ {result.status_code}  {result.error}"
                                )
                                if result.error_info is not None:
                                    display.log_error(result.error_info)

                    overall_success = all(r.success for r in results)
                    if config.mock:
                        break

                    _goal_replan_ctx = ""
                    if overall_success and answer_agent is not None:
                        satisfied, answer_text = await answer_agent.synthesize(
                            safe_intent, results
                        )
                        display.log_thought(
                            f"{'▶ ' if satisfied else '✗ Goal not met: '}{answer_text}"
                        )
                        if satisfied:
                            display.set_answer(answer_text)
                            break
                        overall_success = False
                        _goal_replan_ctx = (
                            f"API returned data but goal was not achieved: {answer_text}"
                        )

                    if overall_success:
                        break
                    assert executor is not None
                    replan_ctx = (
                        _goal_replan_ctx or _build_replan_context(executor, results)
                    )

                # ── Save conversation memory ─────────────────────────────
                if overall_success:
                    if config.mock:
                        saved_messages: list[ConvoMessage] = [
                            ConvoMessage(
                                role="user",
                                content=f"[mock] {plan.action} on "
                                        f"{plan.target_domain}{plan.target_endpoints[0]}",
                            ),
                            ConvoMessage(
                                role="assistant",
                                content=f"[mock] {len(results)} step(s) executed",
                            ),
                        ]
                    elif last_planner is not None and last_planner.last_messages:
                        saved_messages = [
                            ConvoMessage(**m) for m in last_planner.last_messages
                        ]
                    else:
                        saved_messages = []
                    if saved_messages:
                        now = datetime.now(UTC)
                        result_payload = {
                            "endpoint":    results[-1].endpoint    if results else None,
                            "status_code": results[-1].status_code if results else None,
                            "success":     results[-1].success     if results else False,
                        }
                        existing = await convos.get_latest_for_intent(safe_intent)
                        convo_id = existing.id if existing is not None else str(uuid.uuid4())
                        convo_created_at = existing.created_at if existing is not None else now
                        convo = Convo(
                            id=convo_id,
                            intent=safe_intent,
                            created_at=convo_created_at,
                            updated_at=now,
                            messages=saved_messages,
                            result=result_payload,
                        )
                        await convos.save(convo)
                        display.log_thought("Saved conversation memory.")

                if overall_success:
                    await session.persist(host, session_store)

                display.set_status("Complete" if overall_success else "Failed")

    display.log_thought("─" * 60)
    display.log_thought("Pipeline complete.")


# ---------------------------------------------------------------------------
# REPL
# ---------------------------------------------------------------------------

def _print_help() -> None:
    _console.print("\n  [bold]Available commands:[/bold]")
    rows = [
        ("/help",             "Show this help"),
        ("/provider [NAME]",  "Switch LLM provider (re-prompts key + model)"),
        ("/key",              "Re-enter API key"),
        ("/model [NAME]",     "Override the active model"),
        ("/mock",             "Toggle mock mode on/off"),
        ("/target [URL]",     "Override AI-predicted target for next run (empty to clear)"),
        ("/history",          "List stored conversation memories"),
        ("/clear",            "Clear the terminal"),
        ("/quit  /exit",      "Exit Web in the Shell"),
    ]
    for cmd, desc in rows:
        _console.print(f"  [cyan]{cmd:<22}[/cyan] {desc}")
    _console.print()


async def _handle_command(raw: str, config: SessionConfig) -> bool:
    """Process a /command. Returns False if the REPL should exit."""
    parts = raw[1:].split(maxsplit=1)
    cmd   = parts[0].lower() if parts else ""
    arg   = parts[1].strip() if len(parts) > 1 else ""

    if cmd in ("quit", "exit", "q"):
        return False

    if cmd == "help":
        _print_help()

    elif cmd == "clear":
        _console.clear()

    elif cmd == "mock":
        config.mock = not config.mock
        state = "[green]ON[/green]" if config.mock else "[dim]OFF[/dim]"
        _console.print(f"  Mock mode: {state}")

    elif cmd == "target":
        config.target = arg
        if arg:
            _console.print(f"  Target override: [cyan]{arg}[/cyan]")
        else:
            _console.print("  Target cleared — AI will determine target from your goal.")

    elif cmd == "model":
        config.model = arg or None
        if config.model:
            _console.print(f"  Model: [cyan]{config.model}[/cyan]")
        else:
            _console.print(f"  Model reset to default for {config.provider}.")

    elif cmd == "key":
        try:
            key = getpass.getpass("  Enter API key: ").strip()
        except KeyboardInterrupt:
            return True
        if key:
            config.api_key = key
            _console.print("  [green]API key updated.[/green]")

    elif cmd == "provider":
        name = arg.lower()
        if name and name not in DEFAULT_MODELS:
            _console.print(
                f"  Unknown provider: [red]{name!r}[/red]  "
                f"Options: {', '.join(DEFAULT_MODELS)}"
            )
            return True
        if not name:
            providers = list(DEFAULT_MODELS)
            for i, p in enumerate(providers, 1):
                suffix = "  [dim](local)[/dim]" if p == "ollama" else ""
                _console.print(f"  [cyan]{i}[/cyan]. {p}{suffix}")
            choice = IntPrompt.ask("  Choice", default=1)
            name = providers[max(1, min(choice, len(providers))) - 1]

        prev_provider, prev_model, prev_api_key = config.provider, config.model, config.api_key
        config.provider = name
        config.model    = None
        config.api_key  = ""

        if name == "ollama":
            if not await _check_ollama():
                config.provider = prev_provider
                config.model = prev_model
                config.api_key = prev_api_key
                _console.print(
                    "  Ollama not found — start with `ollama serve`. Reverted to previous provider."
                )
                return True
            config.api_key = "ollama"
        else:
            env_var = PROVIDER_ENV_VARS.get(name, "")
            if env_var and os.environ.get(env_var):
                config.api_key = os.environ[env_var]
                _console.print(f"  [green]✓ {env_var} found[/green]")
            else:
                try:
                    key = getpass.getpass(f"  Enter {name} API key: ").strip()
                except KeyboardInterrupt:
                    return True
                if not key:
                    _console.print("  No key entered — provider switch aborted.")
                    return True
                config.api_key = key

        with _console.status("  Fetching models…"):
            models = await fetch_available_models(config.provider, config.api_key)
        default_model = DEFAULT_MODELS.get(config.provider, "")
        if len(models) <= 1:
            config.model = models[0] if models else default_model
            _console.print(f"  Model: [cyan]{config.model}[/cyan]")
        else:
            _console.print(f"  [bold]Available models ({config.provider}):[/bold]")
            default_idx = next((i for i, m in enumerate(models, 1) if m == default_model), 1)
            for i, m in enumerate(models[:20], 1):
                marker = "  [dim](recommended)[/dim]" if m == default_model else ""
                _console.print(f"  [cyan]{i}[/cyan]. {m}{marker}")
            choice = IntPrompt.ask("  Model", default=default_idx)
            config.model = models[min(max(choice, 1), len(models)) - 1]
        _console.print(
            f"  Switched to [cyan]{config.provider}[/cyan] / [cyan]{config.model}[/cyan]"
        )

    elif cmd == "history":
        await _memory_list()

    else:
        _console.print(f"  Unknown command: [red]/{cmd}[/red]  Type /help for a list.")

    return True


async def _repl(config: SessionConfig) -> None:
    """Persistent REPL — accepts goals or /commands until the user quits."""
    mode_hint = "[dim]mock · [/dim]" if config.mock else ""
    _console.print(f"  {mode_hint}Type a goal or [dim]/help[/dim] for commands.\n")

    while True:
        try:
            raw = _console.input("  [bold cyan]wits ❯[/bold cyan] ").strip()
        except (EOFError, KeyboardInterrupt):
            break

        if not raw:
            continue

        if raw.startswith("/"):
            if not await _handle_command(raw, config):
                break
            continue

        intent  = sanitize_for_llm(raw)
        display = AgentDisplay()
        with display:
            try:
                await _run(config, display, intent)
            except KeyboardInterrupt:
                display.set_status("Interrupted")
                display.log_thought("Interrupted.")
            except Exception as exc:
                _handle_api_error(exc, display)
            # Interactive runs: hold the full-screen display until the operator
            # presses Enter so results stay readable before teardown.
            await display.wait_for_enter()
        display.print_result_card()

    _console.print("\n  Goodbye.\n")


# ---------------------------------------------------------------------------
# Memory subcommand
# ---------------------------------------------------------------------------

async def _memory_list() -> None:
    db_path = DEFAULT_DB_PATH
    if not db_path.exists():
        _console.print("(no conversations stored)")
        return
    await init_db(db_path)
    async with ConvoStore(db_path) as store:
        convos = await store.list_all()
    if not convos:
        _console.print("(no conversations stored)")
        return
    table = Table(title="Stored conversations")
    table.add_column("Intent",  style="cyan", overflow="fold")
    table.add_column("ID",      style="dim")
    table.add_column("Updated", style="green")
    table.add_column("Msgs",    justify="right")
    for convo in convos:
        table.add_row(
            convo.intent,
            convo.id,
            convo.updated_at.isoformat(timespec="seconds"),
            str(len(convo.messages)),
        )
    _console.print(table)


async def _memory_clear(intent: str) -> None:
    db_path = DEFAULT_DB_PATH
    if not db_path.exists():
        _console.print("(no conversations stored)")
        return
    await init_db(db_path)
    async with ConvoStore(db_path) as store:
        deleted = await store.clear(intent)
    _console.print(f"Deleted {deleted} conversation(s) for intent: {intent}")


async def _memory_clear_all(*, interactive: bool) -> None:
    db_path = DEFAULT_DB_PATH
    if not db_path.exists():
        _console.print("(no conversations stored)")
        return
    if interactive and not Confirm.ask("Clear ALL stored conversations?", default=False):
        _console.print("Aborted.")
        return
    await init_db(db_path)
    async with ConvoStore(db_path) as store:
        deleted = await store.clear_all()
    _console.print(f"Deleted {deleted} conversation(s).")


async def _run_memory(args: argparse.Namespace, parser: argparse.ArgumentParser) -> None:
    cmd  = args.memory[0]
    rest = args.memory[1:]
    if cmd == "list":
        if rest:
            parser.error("--memory list takes no arguments")
        await _memory_list()
    elif cmd == "clear":
        if not rest:
            parser.error("--memory clear requires an INTENT argument")
        await _memory_clear(" ".join(rest))
    elif cmd == "clear-all":
        if rest:
            parser.error("--memory clear-all takes no arguments")
        await _memory_clear_all(interactive=not args.no_interactive)
    else:
        parser.error(
            f"unknown --memory command: {cmd!r}  "
            "(use list | clear INTENT | clear-all)"
        )


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------

async def main() -> None:
    parser = _build_parser()
    args   = parser.parse_args()

    if args.memory:
        await _run_memory(args, parser)
        return

    try:
        config = await _build_config_async(args, parser)
    except (KeyboardInterrupt, SystemExit):
        return

    # ── Single-shot non-interactive run (CI / scripts) ───────────────────
    if args.no_interactive and args.intent:
        display = AgentDisplay()
        with display:
            try:
                await _run(config, display, args.intent)
            except KeyboardInterrupt:
                display.set_status("Interrupted")
            except Exception as exc:
                _handle_api_error(exc, display)
        display.print_result_card()
        return

    # ── Interactive REPL session ─────────────────────────────────────────
    try:
        await _repl(config)
    except KeyboardInterrupt:
        pass

    if not args.no_interactive:
        try:
            from tui.memory import manage_memory
            await manage_memory(DEFAULT_DB_PATH, _console)
        except KeyboardInterrupt:
            pass


def _cli() -> None:
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        pass


if __name__ == "__main__":
    _cli()
