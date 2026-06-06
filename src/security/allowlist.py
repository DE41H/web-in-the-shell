"""SSRF guard — validates that a URL does not target private or metadata endpoints."""

from __future__ import annotations

import ipaddress
import urllib.parse

_BLOCKED_HOSTNAMES: frozenset[str] = frozenset({
    "localhost",
    "metadata.google.internal",
    "169.254.169.254",   # AWS/GCP/Azure IMDS (hostname form)
    "fd00:ec2::254",     # AWS IMDSv2 IPv6
})

_BLOCKED_SCHEMES: frozenset[str] = frozenset({"file", "gopher", "ftp"})

_PRIVATE_NETWORKS: tuple[ipaddress.IPv4Network | ipaddress.IPv6Network, ...] = (
    ipaddress.ip_network("10.0.0.0/8"),
    ipaddress.ip_network("172.16.0.0/12"),
    ipaddress.ip_network("192.168.0.0/16"),
    ipaddress.ip_network("169.254.0.0/16"),   # link-local
    ipaddress.ip_network("127.0.0.0/8"),       # loopback (covers 127.0.0.1)
    ipaddress.ip_network("::1/128"),            # IPv6 loopback
    ipaddress.ip_network("fe80::/10"),          # IPv6 link-local
    # 0.0.0.0/8 — connects to localhost on Linux
    ipaddress.ip_network("0.0.0.0/8"),
    # IPv6-mapped IPv4 private addresses (::ffff:127.x.x.x, ::ffff:10.x.x.x, etc.)
    ipaddress.ip_network("::ffff:0:0/96"),      # all IPv4-mapped IPv6
    # IPv6 unique-local (fc00::/7 covers fd00::/8 and fc00::/8, RFC 4193)
    ipaddress.ip_network("fc00::/7"),
    # CGNAT / shared address space (RFC 6598)
    ipaddress.ip_network("100.64.0.0/10"),
)


def validate_url(url: str) -> str:
    """Raise ValueError if *url* targets a private/SSRF-dangerous host. Return *url* if safe."""
    if url.startswith("//"):
        raise ValueError(f"Blocked: protocol-relative or scheme-less URL: {url!r}")

    parsed = urllib.parse.urlparse(url)

    if parsed.scheme == "":
        raise ValueError(f"Blocked: protocol-relative or scheme-less URL: {url!r}")

    if parsed.scheme in _BLOCKED_SCHEMES:
        raise ValueError(f"Blocked scheme: {parsed.scheme!r} in URL {url!r}")

    host = parsed.hostname or ""

    if not host:
        raise ValueError(f"URL has no resolvable hostname: {url!r}")

    # Strip IPv6 brackets that urlparse may leave behind
    host_clean = host.strip("[]").lower()

    if host_clean in _BLOCKED_HOSTNAMES:
        raise ValueError(f"Blocked hostname: {host_clean!r} in URL {url!r}")

    try:
        addr = ipaddress.ip_address(host_clean)
    except ValueError:
        # Not an IP literal — hostname checks above are sufficient
        return url

    for network in _PRIVATE_NETWORKS:
        if addr in network:
            raise ValueError(f"Blocked private/reserved address: {addr} in URL {url!r}")

    return url
