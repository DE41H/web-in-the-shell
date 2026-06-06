import pytest
import httpx
import respx

from network.dispatch.client import DispatchClient
from network.session.manager import SessionManager
from network.dispatch.request_builder import RequestSpec, FilePart


@pytest.mark.integration
@pytest.mark.asyncio
async def test_upload_streaming_forwards_async_iterable():
    async def ag():
        yield b"a"
        yield b"b"

    with respx.mock:
        route = respx.post("https://api.test/upload").mock(
            return_value=httpx.Response(200, content=b"ok")
        )

        session = SessionManager()
        async with DispatchClient(session, base_url="https://api.test") as client:
            spec = RequestSpec(method="POST", url="/upload").with_streaming_content(ag())
            resp = await client.send_spec(spec)
            assert resp.status_code == 200

        # respx stores the received request; ensure body was forwarded as concatenated bytes
        assert route.calls
        received = route.calls[0].request.content
        assert received == b"ab"


@pytest.mark.integration
@pytest.mark.asyncio
async def test_multipart_files_sent_correctly():
    f = FilePart(filename="a.txt", content=b"hello", content_type="text/plain")

    with respx.mock:
        route = respx.post("https://api.test/upload").mock(
            return_value=httpx.Response(201, content=b"created")
        )

        session = SessionManager()
        async with DispatchClient(session, base_url="https://api.test") as client:
            spec = RequestSpec(method="POST", url="/upload").with_file(
                "file", f.filename, f.content, f.content_type
            )
            resp = await client.send_spec(spec)
            assert resp.status_code == 201

        assert route.calls
        body = route.calls[0].request.content
        # multipart content should include filename and file content
        assert b"filename=\"a.txt\"" in body
        assert b"hello" in body


class _AsyncStream:
    def __init__(self, chunks):
        self._chunks = list(chunks)

    async def __aiter__(self):
        for c in self._chunks:
            yield c


@pytest.mark.integration
@pytest.mark.asyncio
async def test_download_streaming_response():
    stream = _AsyncStream([b"x", b"y"])
    with respx.mock:
        respx.get("https://api.test/stream").mock(
            return_value=httpx.Response(200, content=stream)
        )

        session = SessionManager()
        async with DispatchClient(session, base_url="https://api.test") as client:
            resp = await client.get("/stream")
            # httpx.Response.aread() aggregates streaming body
            data = await resp.aread()
            assert data == b"xy"
