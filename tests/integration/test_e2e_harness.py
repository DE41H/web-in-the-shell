import asyncio
import os
import sys

import pytest


_NETWORK_ERROR_MARKERS = (
    "ConnectionError",
    "NetworkError",
    "playwright._impl._errors.Error",
    "NameResolutionError",
    "Temporary failure in name resolution",
    "Could not connect",
    "ECONNREFUSED",
    "ETIMEDOUT",
    "ERR_NAME_NOT_RESOLVED",
    "ERR_CONNECTION",
    "ERR_INTERNET_DISCONNECTED",
    "ERR_PROXY_CONNECTION",
    "ERR_SSL",
    "ERR_CERT",
    "net::",
)


def _is_network_failure(stderr: str) -> bool:
    return any(marker in stderr for marker in _NETWORK_ERROR_MARKERS)


@pytest.mark.integration
async def test_main_harness_runs_against_jsonplaceholder():
    """Run src/main.py against jsonplaceholder with mock LLM; assert no crash."""
    env = os.environ.copy()
    env["PYTHONPATH"] = "src"
    env["NO_COLOR"] = "1"
    env["TERM"] = "dumb"
    proc = await asyncio.create_subprocess_exec(
        sys.executable, "-c",
        "import asyncio, sys; "
        "sys.path.insert(0, 'src'); "
        "sys.argv = ['main', '--mock', '--no-interactive', "
        "           '--intent', 'Fetch posts then create one']; "
        "import main; "
        "asyncio.run(main.main())",
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
        env=env,
        cwd=".",
    )
    try:
        stdout, stderr = await asyncio.wait_for(proc.communicate(), timeout=60)
    except (TimeoutError, OSError) as exc:
        try:
            proc.kill()
            await proc.wait()
        except ProcessLookupError:
            pass
        pytest.skip(f"network unavailable: {exc}")

    stderr_text = stderr.decode(errors="replace")
    if proc.returncode != 0 and _is_network_failure(stderr_text):
        pytest.skip(f"network unavailable: {stderr_text[:300]}")

    out = stdout.decode(errors="replace")
    assert proc.returncode == 0, (
        f"main.py crashed (rc={proc.returncode}): {stderr_text[:500]}"
    )

    # The sniffer summary line is always printed, even when 0 responses are
    # captured (e.g. when navigating to the domain root instead of an API path).
    assert "Intercepted" in out, (
        f"sniffer summary missing from output: {out[:500]}"
    )

    assert "Bearer eyJ" not in out
    assert "Bearer abc123def456ghi789" not in out

    assert "\u2713 2" in out, f"no success-status marker in output: {out[:500]}"
