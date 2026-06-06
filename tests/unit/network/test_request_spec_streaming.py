

from network.dispatch.request_builder import RequestSpec, StreamingBody


def test_requestspec_with_bytes_content():
    spec = RequestSpec(method="POST", url="https://x", content=StreamingBody(b"hi"))
    kwargs = spec.to_httpx_kwargs()
    assert kwargs["content"] == b"hi"


def test_requestspec_with_sync_iterable_content():
    def gen():
        yield b"1"
        yield b"2"

    spec = RequestSpec(method="POST", url="https://x", content=StreamingBody(gen()))
    kwargs = spec.to_httpx_kwargs()
    assert hasattr(kwargs["content"], "__iter__")


async def _ag():
    yield b"a"
    yield b"b"


def test_requestspec_with_async_iterable_content():
    ag = _ag()
    spec = RequestSpec(method="POST", url="https://x", content=StreamingBody(ag))
    kwargs = spec.to_httpx_kwargs()
    assert hasattr(kwargs["content"], "__aiter__")
