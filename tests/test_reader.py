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


@pytest.fixture
def public_dns(monkeypatch: pytest.MonkeyPatch) -> None:
    """Resolve every hostname to a public address, so the guard's DNS lookup
    does not need the network. Redirect targets given as IP literals are
    unaffected and still resolve to themselves."""

    def fake_getaddrinfo(host, port, *args, **kwargs):
        return [(2, 1, 6, "", ("93.184.216.34", 0))]

    monkeypatch.setattr(ssrf.socket, "getaddrinfo", fake_getaddrinfo)

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
        respx.get("https://public.example/start").mock(
            return_value=httpx.Response(302, headers={"location": "http://127.0.0.1:3706/"})
        )
        reader = UrlReader()
        with pytest.raises(SsrfError):
            await reader.read("https://public.example/start")

    @pytest.mark.asyncio
    @respx.mock
    async def test_redirect_to_private_lan_is_blocked(self, public_dns) -> None:
        respx.get("https://public.example/start").mock(
            return_value=httpx.Response(302, headers={"location": "http://192.168.86.20:3706/"})
        )
        reader = UrlReader()
        with pytest.raises(SsrfError):
            await reader.read("https://public.example/start")

    @pytest.mark.asyncio
    @respx.mock
    async def test_redirect_to_another_public_host_still_works(self, public_dns) -> None:
        respx.get("https://public.example/start").mock(
            return_value=httpx.Response(302, headers={"location": "https://other.example/end"})
        )
        respx.get("https://other.example/end").mock(
            return_value=httpx.Response(200, text="hello", headers={"content-type": "text/plain"})
        )
        reader = UrlReader()
        out = await reader.read("https://public.example/start")
        assert out["markdown"] == "hello"

    @pytest.mark.asyncio
    @respx.mock
    async def test_redirect_loop_is_bounded(self, public_dns) -> None:
        respx.get("https://loop.example/a").mock(
            return_value=httpx.Response(302, headers={"location": "https://loop.example/a"})
        )
        reader = UrlReader()
        with pytest.raises(ValueError, match="too many redirects"):
            await reader.read("https://loop.example/a")
