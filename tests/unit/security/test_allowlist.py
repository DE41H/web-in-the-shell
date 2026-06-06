import pytest

from security.allowlist import validate_url


# ---- blocked schemes ----

def test_file_scheme_raises():
    with pytest.raises(ValueError):
        validate_url("file:///etc/passwd")


def test_gopher_scheme_raises():
    with pytest.raises(ValueError):
        validate_url("gopher://x")


def test_ftp_scheme_raises():
    with pytest.raises(ValueError):
        validate_url("ftp://x")


def test_https_scheme_returns_unchanged():
    assert validate_url("https://example.com") == "https://example.com"


# ---- blocked hostnames ----

def test_localhost_raises():
    with pytest.raises(ValueError):
        validate_url("http://localhost")


def test_localhost_with_port_raises():
    with pytest.raises(ValueError):
        validate_url("http://localhost:8080/path")


def test_metadata_google_internal_raises():
    with pytest.raises(ValueError):
        validate_url("http://metadata.google.internal/foo")


def test_imds_ipv4_raises():
    with pytest.raises(ValueError):
        validate_url("http://169.254.169.254/latest/meta-data")


def test_imds_ipv6_raises():
    with pytest.raises(ValueError):
        validate_url("http://[fd00:ec2::254]/")


def test_public_hostname_returns_unchanged():
    assert validate_url("http://example.com") == "http://example.com"


# ---- blocked private IPs ----

def test_loopback_127_raises():
    with pytest.raises(ValueError):
        validate_url("http://127.0.0.1")


def test_private_10_dot_raises():
    with pytest.raises(ValueError):
        validate_url("http://10.0.0.1")


def test_private_172_16_raises():
    with pytest.raises(ValueError):
        validate_url("http://172.16.0.1")


def test_private_172_31_edge_raises():
    with pytest.raises(ValueError):
        validate_url("http://172.31.255.255")


def test_private_192_168_raises():
    with pytest.raises(ValueError):
        validate_url("http://192.168.1.1")


def test_ipv6_loopback_raises():
    with pytest.raises(ValueError):
        validate_url("http://[::1]/")


def test_ipv6_link_local_raises():
    with pytest.raises(ValueError):
        validate_url("http://[fe80::1]/")


def test_link_local_169_254_raises():
    with pytest.raises(ValueError):
        validate_url("http://169.254.0.1")


# ---- public IPs pass ----

def test_public_google_dns_returns_unchanged():
    assert validate_url("http://8.8.8.8") == "http://8.8.8.8"


def test_public_cloudflare_returns_unchanged():
    assert validate_url("http://1.1.1.1") == "http://1.1.1.1"


def test_public_ipv6_returns_unchanged():
    assert validate_url("http://[2001:4860:4860::8888]/") == "http://[2001:4860:4860::8888]/"


# ---- edge cases ----

def test_url_without_hostname_raises():
    with pytest.raises(ValueError):
        validate_url("http:///path")
