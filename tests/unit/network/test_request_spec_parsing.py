
import pytest
from pydantic import ValidationError

from network.dispatch.request_builder import RequestSpec


def _sync_gen():
    yield b"a"
    yield b"b"


async def _async_gen():
    yield b"x"
    yield b"y"


def test_requestspec_accepts_raw_bytes_for_content():
    spec = RequestSpec(method="POST", url="https://x", content=b"hello")
    kwargs = spec.to_httpx_kwargs()
    assert kwargs["content"] == b"hello"


def test_requestspec_accepts_sync_iterable_for_content():
    spec = RequestSpec(method="POST", url="https://x", content=_sync_gen())
    kwargs = spec.to_httpx_kwargs()
    assert hasattr(kwargs["content"], "__iter__")


def test_requestspec_accepts_async_iterable_for_content():
    ag = _async_gen()
    spec = RequestSpec(method="POST", url="https://x", content=ag)
    kwargs = spec.to_httpx_kwargs()
    assert hasattr(kwargs["content"], "__aiter__")


def test_with_streaming_content_helper_wraps_values():
    spec = RequestSpec(method="POST", url="https://x")
    new = spec.with_streaming_content(b"abc")
    assert new.content is not None
    assert new.to_httpx_kwargs()["content"] == b"abc"


def test_invalid_content_type_raises_validation_error():
    with pytest.raises(ValidationError):
        RequestSpec(method="POST", url="https://x", content=123)
