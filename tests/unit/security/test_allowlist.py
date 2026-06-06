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


# ---- new private network ranges ----

def test_zero_prefix_0_0_0_0_raises():
    """0.0.0.0/8 connects to localhost on Linux."""
    with pytest.raises(ValueError):
        validate_url("http://0.0.0.1")


def test_zero_prefix_0_0_0_0_exact_raises():
    with pytest.raises(ValueError):
        validate_url("http://0.0.0.0")


def test_ipv6_mapped_ipv4_loopback_raises():
    """::ffff:127.0.0.1 is an IPv4-mapped IPv6 loopback address."""
    with pytest.raises(ValueError):
        validate_url("http://[::ffff:127.0.0.1]")


def test_ipv6_mapped_ipv4_private_10_raises():
    """::ffff:10.0.0.1 is an IPv4-mapped private address."""
    with pytest.raises(ValueError):
        validate_url("http://[::ffff:10.0.0.1]")


def test_ipv6_unique_local_fd00_raises():
    """fd00::1 is in fc00::/7 (RFC 4193 unique-local)."""
    with pytest.raises(ValueError):
        validate_url("http://[fd00::1]")


def test_ipv6_unique_local_fc00_raises():
    """fc00::1 is in fc00::/7 (RFC 4193 unique-local)."""
    with pytest.raises(ValueError):
        validate_url("http://[fc00::1]")


def test_cgnat_100_64_raises():
    """100.64.1.1 is in the CGNAT shared address space (RFC 6598)."""
    with pytest.raises(ValueError):
        validate_url("http://100.64.1.1")


def test_cgnat_100_127_raises():
    """100.127.255.255 is the last address in the CGNAT range."""
    with pytest.raises(ValueError):
        validate_url("http://100.127.255.255")


# ---- protocol-relative URL blocking ----

def test_protocol_relative_url_raises():
    """//attacker.com is a protocol-relative URL and must be blocked."""
    with pytest.raises(ValueError):
        validate_url("//attacker.com")


def test_protocol_relative_url_with_path_raises():
    with pytest.raises(ValueError):
        validate_url("//attacker.com/steal")


# ---- confirm valid public URLs still pass ----

def test_https_example_com_still_passes():
    assert validate_url("https://example.com") == "https://example.com"


def test_http_public_ip_still_passes():
    assert validate_url("http://8.8.8.8") == "http://8.8.8.8"


def test_public_ipv6_still_passes():
    assert validate_url("http://[2001:4860:4860::8888]/") == "http://[2001:4860:4860::8888]/"


# ---- confirm existing private networks still blocked ----

def test_private_10_still_blocked():
    with pytest.raises(ValueError):
        validate_url("http://10.1.2.3")


def test_private_172_16_still_blocked():
    with pytest.raises(ValueError):
        validate_url("http://172.16.0.1")


def test_private_192_168_still_blocked():
    with pytest.raises(ValueError):
        validate_url("http://192.168.0.1")


def test_loopback_127_still_blocked():
    with pytest.raises(ValueError):
        validate_url("http://127.0.0.1")


def test_link_local_169_254_still_blocked():
    with pytest.raises(ValueError):
        validate_url("http://169.254.0.1")


def test_ipv6_loopback_still_blocked():
    with pytest.raises(ValueError):
        validate_url("http://[::1]/")


def test_ipv6_link_local_still_blocked():
    with pytest.raises(ValueError):
        validate_url("http://[fe80::1]/")


def test_schemeless_url_without_slashes_raises():
    with pytest.raises(ValueError):
        validate_url("example.com/path")
