"""Canned CapturedResponse samples for use across tests."""

from __future__ import annotations

from network.intercept.sniffer import CapturedResponse

from tests.conftest import make_captured_response


def post_capture_with_noise() -> CapturedResponse:
    return make_captured_response(
        url="https://api.example.com/posts/1",
        body={
            "id": 1,
            "title": "Hello",
            "body": "World",
            "userId": 1,
            "tracking_id": "abc",
            "metadata": {"nested": "x"},
            "ui_state": "expanded",
        },
    )


def empty_list_capture() -> CapturedResponse:
    return make_captured_response(
        url="https://api.example.com/items",
        body=[],
    )


def populated_list_capture() -> CapturedResponse:
    return make_captured_response(
        url="https://api.example.com/users",
        body=[{"id": 1, "name": "Alice"}, {"id": 2, "name": "Bob"}],
    )


def error_capture(status: int = 500) -> CapturedResponse:
    return make_captured_response(
        url="https://api.example.com/fail",
        status=status,
        body={"error": "internal"},
    )
