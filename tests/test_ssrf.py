"""Unit tests for the SSRF guard (src/mcp_searxng/clients/ssrf.py).

Hermetic: the DNS-resolution cases monkeypatch socket.getaddrinfo so no real
network lookups happen. IP-literal cases need no patching.
"""

import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))

from mcp_searxng.clients import ssrf  # noqa: E402
from mcp_searxng.clients.ssrf import ResolvedUrl, SsrfError, resolve_url, validate_url  # noqa: E402


def _patch_resolve(monkeypatch, ip: str) -> None:
    """Force getaddrinfo to return a single IPv4 address."""

    def fake_getaddrinfo(host, port, *args, **kwargs):
        return [(2, 1, 6, "", (ip, 0))]

    monkeypatch.setattr(ssrf.socket, "getaddrinfo", fake_getaddrinfo)


# --- schemes -----------------------------------------------------------------


@pytest.mark.parametrize("url", ["file:///etc/passwd", "ftp://example.com/x", "gopher://x"])
def test_rejects_non_http_schemes(url):
    with pytest.raises(SsrfError):
        validate_url(url)


def test_rejects_missing_host():
    with pytest.raises(SsrfError):
        validate_url("http:///nohost")


# --- IP literals (no DNS) ----------------------------------------------------


@pytest.mark.parametrize(
    "url",
    [
        "http://127.0.0.1/",
        "http://127.0.0.1:8080/admin",
        "http://10.0.0.5/",
        "http://192.168.1.1/",
        "http://172.16.0.1/",
        "http://169.254.169.254/latest/meta-data/",  # cloud metadata
        "http://0.0.0.0/",
        "http://[::1]/",  # IPv6 loopback
        "http://[fe80::1]/",  # IPv6 link-local
        "https://255.255.255.255/",  # reserved
        # Carrier-grade NAT, 100.64.0.0/10: Tailscale's whole address pool. Not
        # private, loopback, link-local, or reserved by Python's flags, so only
        # the is_global check catches it.
        "http://100.64.0.1/",
        "http://100.100.100.100:8080/",
        "http://100.127.255.254/",
        "http://192.0.0.8/",  # IETF protocol assignments, also non-global
        "http://198.18.0.1/",  # benchmarking range, also non-global
    ],
)
def test_rejects_private_and_reserved_ip_literals(url):
    with pytest.raises(SsrfError):
        validate_url(url)


def test_allows_public_ip_literal():
    assert validate_url("http://1.1.1.1/") == "http://1.1.1.1/"


# --- DNS resolution ----------------------------------------------------------


def test_rejects_hostname_resolving_to_private(monkeypatch):
    _patch_resolve(monkeypatch, "127.0.0.1")
    with pytest.raises(SsrfError):
        validate_url("http://evil.example.com/")


def test_rejects_hostname_resolving_to_metadata(monkeypatch):
    _patch_resolve(monkeypatch, "169.254.169.254")
    with pytest.raises(SsrfError):
        validate_url("http://rebind.example.com/")


def test_allows_hostname_resolving_to_public(monkeypatch):
    _patch_resolve(monkeypatch, "93.184.216.34")  # example.com public range
    assert validate_url("https://example.com/page") == "https://example.com/page"


def test_rejects_unresolvable_host(monkeypatch):
    def boom(host, port, *args, **kwargs):
        raise ssrf.socket.gaierror("name resolution failed")

    monkeypatch.setattr(ssrf.socket, "getaddrinfo", boom)
    with pytest.raises(SsrfError):
        validate_url("http://does-not-exist.invalid/")


def test_rejects_hostname_resolving_to_tailscale_range(monkeypatch):
    _patch_resolve(monkeypatch, "100.100.100.100")
    with pytest.raises(SsrfError, match="private or reserved"):
        validate_url("http://nix1.tailnet.example/")


# --- resolve_url: the address the fetch must pin to -------------------------


def test_resolve_url_returns_the_checked_address_for_a_hostname(monkeypatch):
    _patch_resolve(monkeypatch, "93.184.216.34")
    assert resolve_url("https://example.com/page") == ResolvedUrl(
        url="https://example.com/page", host="example.com", address="93.184.216.34"
    )


def test_resolve_url_returns_the_literal_itself():
    assert resolve_url("http://1.1.1.1:8080/x") == ResolvedUrl(
        url="http://1.1.1.1:8080/x", host="1.1.1.1", address="1.1.1.1"
    )


def test_resolve_url_rejects_if_any_address_is_blocked(monkeypatch):
    def fake_getaddrinfo(host, port, *args, **kwargs):
        return [
            (2, 1, 6, "", ("93.184.216.34", 0)),
            (2, 1, 6, "", ("192.168.86.20", 0)),
        ]

    monkeypatch.setattr(ssrf.socket, "getaddrinfo", fake_getaddrinfo)
    with pytest.raises(SsrfError):
        resolve_url("http://split-horizon.example/")
