"""Tests for src/jsonschema/__init__.py — the local jsonschema shim."""
from __future__ import annotations

import pytest

from jsonschema import ValidationError, validate


# ---- object type ----

def test_object_type_accepts_dict():
    validate({"a": 1}, {"type": "object"})


def test_object_type_rejects_non_dict():
    with pytest.raises(ValidationError):
        validate("not a dict", {"type": "object"})


def test_object_type_rejects_list():
    with pytest.raises(ValidationError):
        validate([1, 2], {"type": "object"})


def test_required_field_missing_raises():
    schema = {"type": "object", "required": ["name"], "properties": {}}
    with pytest.raises(ValidationError):
        validate({}, schema)


def test_required_field_present_passes():
    schema = {"type": "object", "required": ["name"], "properties": {}}
    validate({"name": "Alice"}, schema)


def test_nested_property_validated_recursively():
    schema = {
        "type": "object",
        "properties": {
            "age": {"type": "integer"}
        },
    }
    with pytest.raises(ValidationError):
        validate({"age": "not-a-number"}, schema)


# ---- array type ----

def test_array_type_accepts_list():
    validate([1, 2, 3], {"type": "array"})


def test_array_type_rejects_non_list():
    with pytest.raises(ValidationError):
        validate("not a list", {"type": "array"})


def test_array_items_validated():
    schema = {"type": "array", "items": {"type": "string"}}
    with pytest.raises(ValidationError):
        validate([1, 2, 3], schema)


def test_array_items_valid_passes():
    schema = {"type": "array", "items": {"type": "string"}}
    validate(["a", "b"], schema)


# ---- string type ----

def test_string_type_accepts_str():
    validate("hello", {"type": "string"})


def test_string_type_rejects_non_str():
    with pytest.raises(ValidationError):
        validate(42, {"type": "string"})


def test_string_pattern_match_passes():
    validate("abc123", {"type": "string", "pattern": r"^\w+$"})


def test_string_pattern_mismatch_raises():
    with pytest.raises(ValidationError):
        validate("hello world", {"type": "string", "pattern": r"^\w+$"})


def test_string_enum_match_passes():
    validate("red", {"type": "string", "enum": ["red", "green", "blue"]})


def test_string_enum_mismatch_raises():
    with pytest.raises(ValidationError):
        validate("yellow", {"type": "string", "enum": ["red", "green", "blue"]})


# ---- integer / number type ----

def test_integer_type_accepts_int():
    validate(42, {"type": "integer"})


def test_number_type_accepts_float():
    validate(3.14, {"type": "number"})


def test_integer_type_rejects_string():
    with pytest.raises(ValidationError):
        validate("42", {"type": "integer"})


def test_number_type_rejects_none():
    with pytest.raises(ValidationError):
        validate(None, {"type": "number"})


# ---- unknown / missing type ----

def test_unknown_type_passes_silently():
    validate("anything", {"type": "boolean"})


def test_non_dict_schema_passes_silently():
    validate("anything", "not-a-dict")  # type: ignore[arg-type]


# ---- validate() public entry point ----

def test_validate_raises_validation_error_not_other():
    with pytest.raises(ValidationError):
        validate([], {"type": "object"})


def test_validate_returns_none_on_success():
    result = validate({"x": 1}, {"type": "object"})
    assert result is None
