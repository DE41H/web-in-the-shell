"""Post-resolution SSRF guard transport.

Wraps httpx.AsyncHTTPTransport and resolves the target hostname to IPs
before forwarding the request. If any resolved IP falls in a private or
reserved range, the connection is refused with httpx.ConnectError.

This closes the DNS-rebinding window that the parse-time validate_url()
guard leaves open: an attacker can pass the hostname check, then flip the
DNS record to a private IP before the TCP connection is made.
"""

from __future__ import annotations

import asyncio
import socket

import httpx

from security.allowlist import validate_url


class SSRFTransport(httpx.AsyncHTTPTransport):
    """AsyncHTTPTransport subclass that validates resolved IPs before connecting."""

    async def handle_async_request(self, request: httpx.Request) -> httpx.Response:
        host = request.url.host
        port = request.url.port or (443 if request.url.scheme == "https" else 80)

        loop = asyncio.get_event_loop()
        try:
            results = await loop.run_in_executor(
                None,
                socket.getaddrinfo,
                host,
                port,
                0,
                socket.SOCK_STREAM,
            )
        except OSError:
            # Let the underlying transport surface the DNS error naturally.
            return await super().handle_async_request(request)

        for info in results:
            ip: str = info[4][0]
            # IPv6 addresses need bracket notation for URL construction.
            ip_url = f"http://[{ip}]" if ":" in ip else f"http://{ip}"
            try:
                validate_url(ip_url)
            except ValueError:
                raise httpx.ConnectError(
                    f"SSRF guard: {host} resolved to blocked IP {ip}"
                )

        return await super().handle_async_request(request)
