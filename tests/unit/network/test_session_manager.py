from unittest.mock import AsyncMock, MagicMock

from network.session.manager import (
    _BEARER_RE,
    _CSRF_HEADERS,
    _MIRROR_HEADERS,
    SessionCredentials,
    SessionManager,
)


# ---- as_headers ----

def test_credentials_as_headers_no_token():
    creds = SessionCredentials()
    assert creds.as_headers() == {}


def test_credentials_as_headers_with_bearer():
    creds = SessionCredentials(bearer_token="abc")
    assert creds.as_headers() == {"Authorization": "Bearer abc"}


def test_credentials_as_headers_with_csrf():
    creds = SessionCredentials(csrf_token="xyz")
    assert creds.as_headers() == {"X-CSRF-Token": "xyz"}


def test_credentials_as_headers_with_both():
    creds = SessionCredentials(bearer_token="abc", csrf_token="xyz")
    headers = creds.as_headers()
    assert headers["Authorization"] == "Bearer abc"
    assert headers["X-CSRF-Token"] == "xyz"


def test_credentials_as_headers_preserves_extra():
    creds = SessionCredentials(extra_headers={"X-Api-Key": "k"})
    assert creds.as_headers() == {"X-Api-Key": "k"}


# ---- readiness (has any auth material) ----

def _has_material(creds):
    return bool(
        creds.cookies or creds.bearer_token or creds.csrf_token or creds.extra_headers
    )


def test_credentials_ready_false_when_empty():
    assert _has_material(SessionCredentials()) is False


def test_credentials_ready_true_with_cookies():
    assert _has_material(SessionCredentials(cookies={"x": "1"})) is True


def test_credentials_ready_true_with_token():
    assert _has_material(SessionCredentials(bearer_token="abc")) is True


# ---- module-level constants ----

def test_bearer_extraction_via_regex():
    m = _BEARER_RE.search("Bearer abc.def-ghi+123==")
    assert m is not None
    assert m.group(1) == "abc.def-ghi+123=="
    m2 = _BEARER_RE.search("bearer abc.def-ghi+123==")
    assert m2 is not None
    assert m2.group(1) == "abc.def-ghi+123=="


def test_csrf_header_detection():
    assert isinstance(_CSRF_HEADERS, set)
    assert "x-csrf-token" in _CSRF_HEADERS
    assert "x-xsrf-token" in _CSRF_HEADERS


def test_extra_headers_mirror_set():
    assert "x-api-key" in _MIRROR_HEADERS
    assert "x-client-id" in _MIRROR_HEADERS
    assert "x-app-version" in _MIRROR_HEADERS
    assert "x-requested-with" in _MIRROR_HEADERS


# ---- SessionManager default state ----

def test_session_manager_default_credentials():
    sm = SessionManager()
    assert sm.credentials.cookies == {}
    assert sm.credentials.bearer_token is None
    assert sm.credentials.csrf_token is None
    assert sm.credentials.extra_headers == {}


# ---- SessionManager.attach ----

def test_session_manager_attach_registers_request_handler():
    sm = SessionManager()
    page = MagicMock()
    sm.attach(page)
    page.on.assert_called_once_with("request", sm._on_request)


# ---- SessionManager.sync_cookies ----

async def test_sync_cookies_populates_credentials():
    sm = SessionManager()
    page = MagicMock()
    page.context.cookies = AsyncMock(
        return_value=[
            {"name": "session", "value": "xyz"},
            {"name": "csrf", "value": "abc"},
        ]
    )
    await sm.sync_cookies(page)
    assert sm.credentials.cookies == {"session": "xyz", "csrf": "abc"}


async def test_sync_cookies_empty():
    sm = SessionManager()
    page = MagicMock()
    page.context.cookies = AsyncMock(return_value=[])
    await sm.sync_cookies(page)
    assert sm.credentials.cookies == {}


# ---- SessionManager._on_request ----

async def test_on_request_extracts_bearer_token():
    sm = SessionManager()
    request = MagicMock()
    request.headers = {"authorization": "Bearer t0k3n-value"}
    await sm._on_request(request)
    assert sm.credentials.bearer_token == "t0k3n-value"


async def test_on_request_extracts_csrf_token():
    sm = SessionManager()
    request = MagicMock()
    request.headers = {"x-csrf-token": "abc"}
    await sm._on_request(request)
    assert sm.credentials.csrf_token == "abc"


async def test_on_request_extracts_extra_headers():
    sm = SessionManager()
    request = MagicMock()
    request.headers = {"x-api-key": "k1", "x-client-id": "c1"}
    await sm._on_request(request)
    assert sm.credentials.extra_headers == {"x-api-key": "k1", "x-client-id": "c1"}


async def test_on_request_no_headers():
    sm = SessionManager()
    request = MagicMock()
    request.headers = {}
    await sm._on_request(request)
    assert sm.credentials.bearer_token is None
    assert sm.credentials.csrf_token is None
    assert sm.credentials.extra_headers == {}
