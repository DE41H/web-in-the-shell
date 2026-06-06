"""LLM input sanitizer — strips control chars, prompt injection patterns, and truncates."""

from __future__ import annotations

import re

# Control characters excluding \t (\x09) and \n (\x0a)
_CONTROL_CHARS = re.compile(r"[\x00-\x08\x0b\x0c\x0e-\x1f\x7f]")

# Unicode bidi override characters (U+202A-U+202E, U+2066-U+2069) — Trojan Source attacks
_BIDI_RE = re.compile(r"[‪-‮⁦-⁩]")

# Common prompt injection openers — match the line from its start (after stripping leading space)
_INJECTION_LINE = re.compile(
    r"^\s*(?:"
    r"ignore\s+previous"
    r"|system\s*:"
    r"|<\|im_start\|>"
    r"|#{1,}\s*instruction"
    r"|'\s*;\s*(?:drop|truncate|delete|insert|update)\s+"
    r"|union\s+select\b"
    r"|\$\([^)]*\)"
    r"|`[^`]*`"
    r")",
    re.IGNORECASE | re.MULTILINE,
)

# Inline injection phrases — case-insensitive, match anywhere in a line
_INLINE_INJECTION_RE = re.compile(
    r"(?i)(ignore\s+(all\s+)?previous\s+instructions?"
    r"|disregard\s+(all\s+)?previous"
    r"|you\s+are\s+now\s+(?:a|an|acting\s+as)"
    r"|system\s*:\s*you\s+are)",
    re.MULTILINE,
)

_TRUNCATION_SUFFIX = "[REDACTED]"
_INLINE_REDACT = "[REDACTED]"
_TRUNCATION_MARKER = "[truncated]"


def sanitize_for_llm(text: str, max_chars: int = 4000) -> str:
    """Return *text* safe to include in an LLM prompt.

    Steps applied in order:
    1. Strip null bytes and non-printable control characters (keeps \\n and \\t).
    2. Strip Unicode bidi override characters (Trojan Source protection).
    3. Drop lines that begin with known prompt-injection patterns.
    4. Replace inline injection phrases with [REDACTED].
    5. Truncate to *max_chars*, appending '[truncated]' if cut.
    """
    text = _CONTROL_CHARS.sub("", text)
    text = _BIDI_RE.sub("", text)

    # Remove injection lines entirely rather than masking them so they cannot
    # be partially reconstructed by the model.
    lines = text.splitlines(keepends=True)
    cleaned_lines = [line for line in lines if not _INJECTION_LINE.match(line)]
    text = "".join(cleaned_lines)

    # Replace inline injection phrases with [REDACTED]
    text = _INLINE_INJECTION_RE.sub(_INLINE_REDACT, text)

    if len(text) > max_chars:
        text = text[:max_chars] + _TRUNCATION_MARKER

    return text
