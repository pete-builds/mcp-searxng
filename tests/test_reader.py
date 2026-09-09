"""Regression tests for redirect handling in the URL reader.

Hermetic: respx intercepts every request, so no network traffic occurs.
"""

import sys
from pathlib import Path

import httpx
import pytest
import respx

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))

from mcp_searxng.clients import ssrf  # noqa: E402
from mcp_searxng.clients.reader import UrlReader  # noqa: E402
from mcp_searxng.clients.ssrf import SsrfError  # noqa: E402


PUBLIC_IP = "93.184.216.34"  # example.com's range


@pytest.fixture
def public_dns(monkeypatch: pytest.MonkeyPatch) -> None:
    """Resolve every hostname to a public address, so the guard's DNS lookup
    does not need the network. Redirect targets given as IP literals are
    unaffected and still resolve to themselves.

    The reader connects to the address the guard vetted, so respx routes below
    are registered against PUBLIC_IP with the hostname in the Host header, which
    is exactly what a real origin would see on the wire."""

    def fake_getaddrinfo(host, port, *args, **kwargs):
        return [(2, 1, 6, "", (PUBLIC_IP, 0))]

    monkeypatch.setattr(ssrf.socket, "getaddrinfo", fake_getaddrinfo)


def pinned(path: str, host: str, scheme: str = "https") -> respx.Route:
    """Route for a request the reader pinned to PUBLIC_IP on behalf of ``host``."""
    return respx.get(f"{scheme}://{PUBLIC_IP}{path}", headers={"host": host})

class TestRedirectHopsAreValidated:
    """A public host must not be able to bounce the fetch to a private address.

    validate_url used to run once, on the caller-supplied URL, while httpx was
    configured with follow_redirects=True. The first host passed the guard and the
    Location header then pointed at loopback or the LAN, so the guard covered the
    one hop that was never the risk.
    """

    @pytest.mark.asyncio
    @respx.mock
    async def test_redirect_to_loopback_is_blocked(self, public_dns) -> None:
        pinned("/start", "public.example").mock(
            return_value=httpx.Response(302, headers={"location": "http://127.0.0.1:3706/"})
        )
        reader = UrlReader()
        with pytest.raises(SsrfError):
            await reader.read("https://public.example/start")

    @pytest.mark.asyncio
    @respx.mock
    async def test_redirect_to_private_lan_is_blocked(self, public_dns) -> None:
        pinned("/start", "public.example").mock(
            return_value=httpx.Response(302, headers={"location": "http://192.168.86.20:3706/"})
        )
        reader = UrlReader()
        with pytest.raises(SsrfError):
            await reader.read("https://public.example/start")

    @pytest.mark.asyncio
    @respx.mock
    async def test_redirect_to_another_public_host_still_works(self, public_dns) -> None:
        pinned("/start", "public.example").mock(
            return_value=httpx.Response(302, headers={"location": "https://other.example/end"})
        )
        pinned("/end", "other.example").mock(
            return_value=httpx.Response(200, text="hello", headers={"content-type": "text/plain"})
        )
        reader = UrlReader()
        out = await reader.read("https://public.example/start")
        assert out["markdown"] == "hello"

    @pytest.mark.asyncio
    @respx.mock
    async def test_redirect_loop_is_bounded(self, public_dns) -> None:
        pinned("/a", "loop.example").mock(
            return_value=httpx.Response(302, headers={"location": "https://loop.example/a"})
        )
        reader = UrlReader()
        with pytest.raises(ValueError, match="too many redirects"):
            await reader.read("https://loop.example/a")


class TestResolvedAddressIsPinned:
    """The fetch must connect to the address the guard checked, not re-resolve.

    The guard used to validate the hostname and then hand the hostname string to
    httpx, which resolved it again on connect. A hostile authoritative server
    with a zero TTL answers "public" to the first lookup and "loopback" to the
    second, and the internal page comes back to the agent (DNS rebinding).
    """

    @pytest.fixture
    def rebinding_dns(self, monkeypatch: pytest.MonkeyPatch) -> list[str]:
        """First lookup says public, every later lookup says loopback. Returns
        the list of hosts looked up so a test can count lookups."""
        lookups: list[str] = []

        def fake_getaddrinfo(host, port, *args, **kwargs):
            lookups.append(host)
            ip = PUBLIC_IP if len(lookups) == 1 else "127.0.0.1"
            return [(2, 1, 6, "", (ip, 0))]

        monkeypatch.setattr(ssrf.socket, "getaddrinfo", fake_getaddrinfo)
        return lookups

    @pytest.mark.asyncio
    @respx.mock
    async def test_fetch_goes_to_the_vetted_address_and_resolves_once(
        self, rebinding_dns
    ) -> None:
        public = pinned("/page", "public.example").mock(
            return_value=httpx.Response(
                200, text="public page", headers={"content-type": "text/plain"}
            )
        )
        internal = respx.get("https://127.0.0.1/page").mock(
            return_value=httpx.Response(
                200, text="INTERNAL SECRET PAGE", headers={"content-type": "text/plain"}
            )
        )
        reader = UrlReader()
        out = await reader.read("https://public.example/page")

        assert out["markdown"] == "public page"
        assert not internal.called
        assert public.called
        assert rebinding_dns == ["public.example"], "hostname must be resolved exactly once"

    @pytest.mark.asyncio
    @respx.mock
    async def test_tls_server_name_is_the_hostname_not_the_address(
        self, public_dns
    ) -> None:
        route = pinned("/page", "public.example").mock(
            return_value=httpx.Response(200, text="ok", headers={"content-type": "text/plain"})
        )
        reader = UrlReader()
        await reader.read("https://public.example/page")

        request = route.calls.last.request
        assert request.url.host == PUBLIC_IP
        assert request.headers["host"] == "public.example"
        assert request.extensions["sni_hostname"] == "public.example"

    @pytest.mark.asyncio
    @respx.mock
    async def test_reported_url_is_the_hostname_form(self, public_dns) -> None:
        pinned("/start", "public.example").mock(
            return_value=httpx.Response(302, headers={"location": "/end"})
        )
        pinned("/end", "public.example").mock(
            return_value=httpx.Response(200, text="ok", headers={"content-type": "text/plain"})
        )
        reader = UrlReader()
        out = await reader.read("https://public.example/start")

        assert out["url"] == "https://public.example/end"

    @pytest.mark.asyncio
    @respx.mock
    async def test_non_default_port_survives_in_host_header(self, public_dns) -> None:
        route = respx.get(f"http://{PUBLIC_IP}:8080/x", headers={"host": "public.example:8080"}).mock(
            return_value=httpx.Response(200, text="ok", headers={"content-type": "text/plain"})
        )
        reader = UrlReader()
        await reader.read("http://public.example:8080/x")

        assert route.called
        assert "sni_hostname" not in route.calls.last.request.extensions
