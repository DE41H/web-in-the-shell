
import pytest

from network.dispatch.request_builder import StreamingBody


def test_streamingbody_accepts_bytes():
    sb = StreamingBody(b"hello")
    assert isinstance(sb.value, bytes)


def test_streamingbody_accepts_sync_iterable():
    def gen():
        yield b"a"
        yield b"b"

    sb = StreamingBody(gen())
    assert hasattr(sb.value, "__iter__")


async def _async_gen():
    yield b"x"
    yield b"y"


def test_streamingbody_accepts_async_iterable():
    # construction should accept an async generator object
    ag = _async_gen()
    sb = StreamingBody(ag)
    assert hasattr(sb.value, "__aiter__")


def test_streamingbody_rejects_invalid_type():
    with pytest.raises(TypeError):
        StreamingBody(object())
