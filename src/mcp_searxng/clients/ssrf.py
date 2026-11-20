"""SSRF guard for user-supplied URLs.

Before fetching any caller-supplied URL, validate it:
  - scheme must be http or https
  - hostname must resolve to a public, routable IP

Rejects literals and DNS results that fall in private, loopback, link-local,
reserved, multicast, or otherwise non-global ranges. This blocks the classic
SSRF pivots (http://169.254.169.254/ cloud metadata, http://127.0.0.1/,
http://10.x/, DNS names that resolve to internal hosts) before any network
fetch happens. "Non-global" matters on its own: 100.64.0.0/10 is carrier-grade
NAT, which Tailscale hands to every node, and none of the private/loopback/
link-local flags cover it.

Resolution covers every address the hostname maps to (IPv4 and IPv6); if any
resolved address is non-public, the whole URL is rejected.

The guard also hands back the address it vetted, so the fetch can connect to
that exact address instead of resolving the hostname a second time. Two
lookups are the DNS-rebinding gap: a hostile authoritative server answers a
public address to the guard and a loopback address to the HTTP client a few
milliseconds later.
"""

import asyncio
import ipaddress
import socket
from dataclasses import dataclass
from urllib.parse import urlsplit

ALLOWED_SCHEMES = {"http", "https"}


class SsrfError(ValueError):
    """Raised when a URL is rejected by the SSRF guard."""


@dataclass(frozen=True)
class ResolvedUrl:
    """A URL the guard accepted, plus the one address it checked.

    ``address`` is what the fetch must connect to. ``host`` is what the fetch
    must keep sending as the ``Host`` header and TLS server name, so the
    origin still sees its own name and certificate verification still runs
    against it.
    """

    url: str
    host: str
    address: str


def _ip_is_blocked(ip: ipaddress._BaseAddress) -> bool:
    """True if the IP falls in a non-public / unsafe range.

    ``is_global`` is the catch-all: it is False for every range the explicit
    flags cover and also for the ones they do not, chiefly 100.64.0.0/10.
    """
    return (
        ip.is_private
        or ip.is_loopback
        or ip.is_link_local
        or ip.is_reserved
        or ip.is_multicast
        or ip.is_unspecified
        or not ip.is_global
    )


def validate_url(url: str) -> str:
    """Validate a URL against the SSRF guard. Returns the URL if safe.

    Raises SsrfError with a human-readable reason if the URL must not be
    fetched (bad scheme, missing host, unresolvable host, or any resolved
    address in a private/loopback/link-local/reserved/multicast range).
    """
    return resolve_url(url).url


def _parse(url: str) -> tuple[str, ipaddress.IPv4Address | ipaddress.IPv6Address | None]:
    """Scheme and host checks shared by the sync and async resolvers.

    Returns the hostname and, when the host is an IP literal, the parsed address.
    """
    parts = urlsplit(url)

    if parts.scheme.lower() not in ALLOWED_SCHEMES:
        raise SsrfError(
            f"scheme {parts.scheme!r} not allowed; only http and https are permitted"
        )

    host = parts.hostname
    if not host:
        raise SsrfError("URL has no host")

    try:
        literal = ipaddress.ip_address(host)
    except ValueError:
        literal = None

    if literal is not None and _ip_is_blocked(literal):
        raise SsrfError(f"host IP {host} is in a private or reserved range")
    return host, literal


def _check_infos(url: str, host: str, infos: list) -> ResolvedUrl:
    """Reject the URL if ANY resolved address is non-public; else pin the first."""
    if not infos:
        raise SsrfError(f"host {host!r} resolved to no addresses")

    checked: list[str] = []
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
        checked.append(str(ip))

    return ResolvedUrl(url=url, host=host, address=checked[0])


def resolve_url(url: str) -> ResolvedUrl:
    """Validate a URL and return it with the address that passed the check.

    Same rejections as :func:`validate_url`. When the host is a name, every
    address it resolves to is checked and the first one is returned as the
    address to connect to; a caller that connects to ``address`` never
    triggers a second lookup, which is what closes the rebinding window.

    Blocking: this does a synchronous DNS lookup. Code running on the event
    loop must use :func:`resolve_url_async` instead.
    """
    host, literal = _parse(url)
    if literal is not None:
        return ResolvedUrl(url=url, host=host, address=str(literal))
    try:
        infos = socket.getaddrinfo(host, None)
    except socket.gaierror as e:
        raise SsrfError(f"could not resolve host {host!r}: {e}") from e
    return _check_infos(url, host, infos)


async def resolve_url_async(url: str) -> ResolvedUrl:
    """:func:`resolve_url` without blocking the event loop.

    ``socket.getaddrinfo`` is a blocking call. Run inline in an async tool, one
    slow resolver answer (a lookup can take the full resolver timeout, several
    seconds) stalled every other session on the server, because they all share
    the one loop. The lookup runs in a worker thread instead. ``socket.getaddrinfo``
    is looked up at call time, so tests that monkeypatch it still apply.
    """
    host, literal = _parse(url)
    if literal is not None:
        return ResolvedUrl(url=url, host=host, address=str(literal))
    try:
        infos = await asyncio.to_thread(socket.getaddrinfo, host, None)
    except socket.gaierror as e:
        raise SsrfError(f"could not resolve host {host!r}: {e}") from e
    return _check_infos(url, host, infos)
