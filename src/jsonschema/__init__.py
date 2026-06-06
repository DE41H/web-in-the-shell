"""A tiny, local jsonschema shim used during tests.

This implements a minimal subset of jsonschema.validate and a
ValidationError exception used by the planner. It's intentionally small and
only supports the schema features needed by ToolSchema.parameters
(type, properties, required, items, pattern, enum).

When running in production, prefer the real `jsonschema` package; this
shim exists so the test environment (which may not have extra deps
installed) behaves deterministically.
"""
from __future__ import annotations

import re
from typing import Any


class ValidationError(Exception):
    pass


def _validate(schema: dict[str, Any], data: Any, path: str = "") -> None:
    if not isinstance(schema, dict):
        return
    stype = schema.get("type")
    if stype == "object":
        if not isinstance(data, dict):
            raise ValidationError(f"Expected object at {path or '/'} but got {type(data).__name__}")
        props = schema.get("properties", {}) or {}
        required = schema.get("required", []) or []
        for r in required:
            if r not in data:
                raise ValidationError(f"Missing required property '{r}' at {path or '/'}")
        for k, v in data.items():
            if k in props:
                _validate(props[k], v, path=f"{path}/{k}")
        return
    if stype == "array":
        if not isinstance(data, list):
            raise ValidationError(f"Expected array at {path or '/'} but got {type(data).__name__}")
        items = schema.get("items")
        if items:
            for i, it in enumerate(data):
                _validate(items, it, path=f"{path}[{i}]")
        return
    if stype == "string":
        if not isinstance(data, str):
            raise ValidationError(f"Expected string at {path or '/'} but got {type(data).__name__}")
        pat = schema.get("pattern")
        if pat:
            if not re.search(pat, data):
                raise ValidationError(f"String at {path or '/'} does not match pattern {pat}")
        if "enum" in schema:
            if data not in schema["enum"]:
                raise ValidationError(
                    f"Value '{data}' at {path or '/'} not in enum {schema['enum']}"
                )
        return
    if stype in ("integer", "number"):
        if not isinstance(data, (int, float)):
            raise ValidationError(f"Expected number at {path or '/'} but got {type(data).__name__}")
        return
    return


def validate(instance: Any, schema: dict[str, Any]) -> None:
    """Validate instance against schema and raise ValidationError on failure."""
    try:
        _validate(schema, instance, path="")
    except ValidationError:
        raise
