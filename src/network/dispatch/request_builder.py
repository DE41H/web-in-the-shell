from __future__ import annotations

from typing import Any
from collections.abc import AsyncIterable, Iterable
from collections.abc import AsyncIterable as AsyncIterABC, Iterable as IterABC
from pydantic import BaseModel, Field, field_validator

# core_schema is provided either by pydantic or pydantic_core depending on
# packaging; attempt both so import succeeds across installations.
try:
    from pydantic import core_schema
except Exception:
    try:
        import pydantic_core.core_schema as core_schema  # type: ignore
    except Exception as e:  # pragma: no cover - explicit runtime requirement
        raise ImportError(
            "web-in-the-shell requires pydantic>=2.1.0 / pydantic_core providing core_schema. "
            "Please upgrade your environment (pip install -U 'pydantic>=2.1.0')."
        ) from e


class FilePart(BaseModel):
    filename: str
    content: bytes
    content_type: str | None = None


class StreamingBody:
    """Wrapper for streaming request bodies.

    Accepts raw bytes, a synchronous Iterable[bytes], or an AsyncIterable[bytes].
    Implements pydantic's __get_pydantic_core_schema__ so it can be used as a
    typed field without turning on arbitrary_types_allowed. We require a
    recent pydantic runtime that exposes core_schema.
    """

    def __init__(self, value: bytes | IterABC[bytes] | AsyncIterABC[bytes]):
        # Validate eagerly for callers constructing StreamingBody directly.
        if isinstance(value, (bytes, bytearray)):
            self.value = bytes(value)
        elif isinstance(value, AsyncIterABC) or isinstance(value, IterABC):
            self.value = value
        elif isinstance(value, StreamingBody):
            # unwrap
            self.value = value.value
        else:
            raise TypeError("StreamingBody requires bytes or (async) iterable of bytes")

    def __repr__(self) -> str:  # pragma: no cover - trivial
        return f"StreamingBody({type(self.value).__name__})"

    @classmethod
    def __get_pydantic_core_schema__(cls, source_type: Any, handler: Any) -> Any:
        return core_schema.no_info_plain_validator_function(cls._validate)

    @staticmethod
    def _validate(v: Any, info: Any = None) -> StreamingBody:
        # Accept bytes directly
        if isinstance(v, StreamingBody):
            return v
        if isinstance(v, (bytes, bytearray)):
            return StreamingBody(bytes(v))
        # Accept async iterables
        if isinstance(v, AsyncIterABC):
            return StreamingBody(v)
        # Accept sync iterables
        if isinstance(v, IterABC):
            return StreamingBody(v)
        # Raise ValueError so pydantic wraps this into a ValidationError
        raise ValueError("StreamingBody requires bytes or (async) iterable of bytes")


class RequestSpec(BaseModel):
    """Canonical request specification for DispatchClient.

    This model accepts high-level fields and converts them into httpx-friendly
    kwargs via `to_httpx_kwargs()`.
    """

    method: str = Field(...)
    url: str | None = None

    @field_validator("method", mode="before")
    @classmethod
    def validate_method(cls, v: str) -> str:
        if "\r" in v or "\n" in v:
            raise ValueError("method must not contain CR or LF characters")
        cleaned = v.strip().upper()
        if not cleaned:
            raise ValueError("method must not be blank")
        return cleaned
    params: dict[str, Any] | None = None
    headers: dict[str, str] | None = None
    # parsed_json accepts the parsed JSON body; alias 'json' kept for callers
    parsed_json: Any | None = Field(default=None, alias="json")
    data: dict[str, Any] | None = None
    files: dict[str, FilePart] | None = None
    # content may be a StreamingBody (which can wrap bytes or (async) iterable)
    content: StreamingBody | None = None
    timeout: float | None = None
    cookies: dict[str, str] | None = None

    # Model config: allow population by field name. We require a modern
    # pydantic that supports core_schema so arbitrary_types are not needed.
    model_config = {"populate_by_name": True}

    def to_httpx_kwargs(self, session_headers: dict[str, str] | None = None,
                        session_cookies: dict[str, str] | None = None) -> dict[str, Any]:
        """Return kwargs suitable for httpx.AsyncClient.request().

        session_headers and session_cookies are merged with per-request fields
        (per-request takes precedence).
        """
        headers = dict(session_headers or {})
        if self.headers:
            headers.update(self.headers)

        cookies = dict(session_cookies or {})
        if self.cookies:
            cookies.update(self.cookies)

        kwargs: dict[str, Any] = {"headers": headers}
        if self.params:
            kwargs["params"] = self.params
        if self.parsed_json is not None:
            kwargs["json"] = self.parsed_json
        if self.data is not None:
            kwargs["data"] = self.data
        if self.content is not None:
            # unwrap StreamingBody to the raw underlying value for httpx
            val = self.content.value if hasattr(self.content, "value") else self.content
            kwargs["content"] = val
        if self.files:
            files_arg = {}
            for name, fp in self.files.items():
                # httpx accepts (filename, content, content_type)
                tup = (fp.filename, fp.content)
                if fp.content_type:
                    tup = (fp.filename, fp.content, fp.content_type)
                files_arg[name] = tup
            kwargs["files"] = files_arg
        if cookies:
            kwargs["cookies"] = cookies
        if self.timeout is not None:
            kwargs["timeout"] = self.timeout
        return kwargs

    def with_file(
        self, name: str, filename: str, content: bytes, content_type: str | None = None
    ) -> RequestSpec:
        files = dict(self.files or {})
        files[name] = FilePart(filename=filename, content=content, content_type=content_type)
        return self.model_copy(update={"files": files})

    def with_streaming_content(
        self, content: bytes | AsyncIterable[bytes] | Iterable[bytes]
    ) -> RequestSpec:
        """Return a copy of this RequestSpec with streaming content set.

        Accepts bytes, sync Iterable[bytes], or AsyncIterable[bytes].
        """
        return self.model_copy(update={"content": StreamingBody(content)})
