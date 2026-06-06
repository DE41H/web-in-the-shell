"""LLM input sanitizer — strips control chars, prompt injection patterns, and truncates."""

import re

# Control characters excluding \t (\x09) and \n (\x0a)
_CONTROL_CHARS = re.compile(r"[\x00-\x08\x0b\x0c\x0e-\x1f\x7f]")

# Common prompt injection openers — match the line from its start (after stripping leading space)
_INJECTION_LINE = re.compile(
    r"^\s*(?:"
    r"ignore\s+previous"
    r"|system\s*:"
    r"|<\|im_start\|>"
    r"|#{1,}\s*instruction"
    r"|'\s*;\s*(?:drop|truncate|delete|insert|update)\s+"
    r"|union\s+select\b"
    r"|\$\([^)]{0,80}\)"
    r"|`[^`]{0,80}`"
    r")",
    re.IGNORECASE | re.MULTILINE,
)

_TRUNCATION_SUFFIX = "[truncated]"


def sanitize_for_llm(text: str, max_chars: int = 4000) -> str:
    """Return *text* safe to include in an LLM prompt.

    Steps applied in order:
    1. Strip null bytes and non-printable control characters (keeps \\n and \\t).
    2. Drop lines that begin with known prompt-injection patterns.
    3. Truncate to *max_chars*, appending '[truncated]' if cut.
    """
    text = _CONTROL_CHARS.sub("", text)

    # Remove injection lines entirely rather than masking them so they cannot
    # be partially reconstructed by the model.
    lines = text.splitlines(keepends=True)
    cleaned_lines = [line for line in lines if not _INJECTION_LINE.match(line)]
    text = "".join(cleaned_lines)

    if len(text) > max_chars:
        text = text[:max_chars] + _TRUNCATION_SUFFIX

    return text
