# syntax=docker/dockerfile:1
# ──────────────────────────────────────────────────────────────────────────────
# Web in the Shell — multi-stage lean image
#
# Build:  podman build -f docker/Dockerfile -t wits .
# Run:    podman run -it wits          # interactive setup → live mode
#         podman run wits --mock       # mock mode, no prompts, no API key
#
# Base pinned to bookworm (Debian 12). python:3.12-slim moved to trixie
# (Debian 13) in mid-2025; Playwright 1.48.0's --with-deps references
# ttf-unifont / ttf-ubuntu-font-family which were removed in trixie.
# Bookworm still ships them as transitional packages and is fully supported
# by Playwright 1.48.0.
# ──────────────────────────────────────────────────────────────────────────────

# ── Stage 1: deps ─────────────────────────────────────────────────────────────
# Resolves and installs all pinned Python deps into an isolated .venv.
# Cached independently — only rebuilds when pyproject.toml or uv.lock change.
FROM python:3.12-slim-bookworm AS deps

COPY --from=ghcr.io/astral-sh/uv:latest /uv /usr/local/bin/uv

ENV UV_NO_CACHE=1 \
    UV_PYTHON_DOWNLOADS=never

WORKDIR /app
COPY pyproject.toml uv.lock ./
RUN uv sync --no-dev --frozen

# ── Stage 2: runtime ──────────────────────────────────────────────────────────
# Fresh bookworm base — no uv, no pip cache, no build artefacts from stage 1.
# Copies only the finished .venv, then installs Playwright's Chromium binary.
FROM python:3.12-slim-bookworm AS runtime

ENV PLAYWRIGHT_BROWSERS_PATH=/usr/local/share/playwright-browsers \
    PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1

WORKDIR /app

# Pre-built venv from stage 1 (same base image — Python interpreter paths match)
COPY --from=deps /app/.venv ./.venv

# Chromium + system libraries; package index wiped after — not needed at runtime.
RUN .venv/bin/playwright install chromium --with-deps \
    && find "${PLAYWRIGHT_BROWSERS_PATH}" -type f \( -name "chrome" -o -name "chromium" \) \
       | xargs -r chmod a+rx \
    && apt-get clean \
    && rm -rf /var/lib/apt/lists/* /tmp/* /var/tmp/*

# Source last — most-changed layer, above the stable Chromium layer.
COPY src/ ./src/

RUN useradd -m -u 1000 wits \
    && chown -R wits:wits /app
USER wits

ENTRYPOINT [".venv/bin/python", "src/main.py"]
CMD []
