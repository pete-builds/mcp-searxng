"""SSRF guard for user-supplied URLs.

Before fetching any caller-supplied URL, validate it:
  - scheme must be http or https
  - hostname must resolve to a public, routable IP

Rejects literals and DNS results that fall in private, loopback, link-local,
reserved, or multicast ranges. This blocks the classic SSRF pivots
(http://169.254.169.254/ cloud metadata, http://127.0.0.1/, http://10.x/,
DNS names that resolve to internal hosts) before any network fetch happens.

Resolution covers every address the hostname maps to (IPv4 and IPv6); if any
resolved address is non-public, the whole URL is rejected.
"""

import ipaddress
import socket
from urllib.parse import urlsplit

ALLOWED_SCHEMES = {"http", "https"}


class SsrfError(ValueError):
    """Raised when a URL is rejected by the SSRF guard."""


def _ip_is_blocked(ip: ipaddress._BaseAddress) -> bool:
    """True if the IP falls in a non-public / unsafe range."""
    return (
        ip.is_private
        or ip.is_loopback
        or ip.is_link_local
        or ip.is_reserved
        or ip.is_multicast
        or ip.is_unspecified
    )


def validate_url(url: str) -> str:
    """Validate a URL against the SSRF guard. Returns the URL if safe.

    Raises SsrfError with a human-readable reason if the URL must not be
    fetched (bad scheme, missing host, unresolvable host, or any resolved
    address in a private/loopback/link-local/reserved/multicast range).
    """
    parts = urlsplit(url)

    if parts.scheme.lower() not in ALLOWED_SCHEMES:
        raise SsrfError(
            f"scheme {parts.scheme!r} not allowed; only http and https are permitted"
        )

    host = parts.hostname
    if not host:
        raise SsrfError("URL has no host")

    # If the host is already an IP literal, check it directly.
    try:
        literal = ipaddress.ip_address(host)
    except ValueError:
        literal = None

    if literal is not None:
        if _ip_is_blocked(literal):
            raise SsrfError(f"host IP {host} is in a private or reserved range")
        return url

    # Otherwise resolve the hostname and check every returned address.
    try:
        infos = socket.getaddrinfo(host, None)
    except socket.gaierror as e:
        raise SsrfError(f"could not resolve host {host!r}: {e}") from e

    if not infos:
        raise SsrfError(f"host {host!r} resolved to no addresses")

    for info in infos:
        addr = info[4][0]
        # Strip IPv6 scope id if present (e.g. "fe80::1%eth0").
        addr = addr.split("%", 1)[0]
        try:
            ip = ipaddress.ip_address(addr)
        except ValueError:
            raise SsrfError(f"host {host!r} resolved to invalid address {addr!r}")
        if _ip_is_blocked(ip):
            raise SsrfError(
                f"host {host!r} resolves to {addr}, which is in a private or reserved range"
            )

    return url
