import pytest
from pydantic import ValidationError

from network.dispatch.request_builder import RequestSpec, FilePart


def test_to_httpx_kwargs_merges_headers_and_cookies():
    spec = RequestSpec(
        method="POST",
        url="https://api.test/x",
        headers={"X-Custom": "1"},
        cookies={"sid": "abc"},
        json={"k": "v"},
    )
    kwargs = spec.to_httpx_kwargs(
        session_headers={"User-Agent": "ua"}, session_cookies={"lang": "en"}
    )
    assert kwargs["headers"]["User-Agent"] == "ua"
    assert kwargs["headers"]["X-Custom"] == "1"
    assert kwargs["cookies"]["lang"] == "en"
    assert kwargs["cookies"]["sid"] == "abc"
    assert kwargs["json"] == {"k": "v"}


def test_blank_method_raises():
    with pytest.raises(ValidationError):
        RequestSpec(method="")


def test_whitespace_only_method_raises():
    with pytest.raises(ValidationError):
        RequestSpec(method="   ")


def test_newline_in_method_raises():
    with pytest.raises(ValidationError):
        RequestSpec(method="GET\r\nX-Injected: evil")


def test_method_normalized_to_uppercase():
    spec = RequestSpec(method="post")
    assert spec.method == "POST"


def test_method_strips_whitespace():
    spec = RequestSpec(method="  get  ")
    assert spec.method == "GET"


def test_files_serialised_to_httpx_format():
    f = FilePart(filename="a.txt", content=b"hello", content_type="text/plain")
    spec = RequestSpec(method="POST", url="https://api.test/upload", files={"file": f})
    kwargs = spec.to_httpx_kwargs()
    assert "files" in kwargs
    assert "file" in kwargs["files"]
    tup = kwargs["files"]["file"]
    assert tup[0] == "a.txt"
    assert tup[1] == b"hello"
    assert tup[2] == "text/plain"
