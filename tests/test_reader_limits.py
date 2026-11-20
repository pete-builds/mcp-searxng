"""read_url resource limits and content gating.

Each test here failed against 0.5.0:

- The 5 MB cap was checked after the whole body had been downloaded, so it
  bounded nothing: a multi-gigabyte response sat in memory first.
- There was no wall-clock deadline. httpx's timeout is per socket read, so a
  server dripping a byte at a time held the fetch open indefinitely.
- A PDF or image was decoded as text and returned as "markdown".
- The SSRF guard's DNS lookup blocked the event loop that every session shares.

Hermetic: respx intercepts every request and DNS is faked.
"""

import asyncio
import sys
import time
from pathlib import Path

import httpx
import pytest
import respx

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))

from mcp_searxng.clients import reader as reader_mod  # noqa: E402
from mcp_searxng.clients import ssrf  # noqa: E402

PUBLIC_IP = "93.184.216.34"
MB = 1_000_000


@pytest.fixture
def public_dns(monkeypatch: pytest.MonkeyPatch) -> None:
    def fake_getaddrinfo(host, port, *args, **kwargs):
        return [(2, 1, 6, "", (PUBLIC_IP, 0))]

    monkeypatch.setattr(ssrf.socket, "getaddrinfo", fake_getaddrinfo)


def route(path: str, host: str = "big.example") -> respx.Route:
    return respx.get(f"https://{PUBLIC_IP}{path}", headers={"host": host})


class Counter:
    """An async body that counts how many chunks the reader actually pulled."""

    def __init__(self, chunks: int, size: int = MB, delay: float = 0.0):
        self.chunks, self.size, self.delay, self.pulled = chunks, size, delay, 0

    async def __aiter__(self):
        for _ in range(self.chunks):
            if self.delay:
                await asyncio.sleep(self.delay)
            self.pulled += 1
            yield b"a" * self.size


class TestSizeCapIsEnforcedWhileReading:
    @pytest.mark.asyncio
    @respx.mock
    async def test_oversize_body_stops_at_the_cap(self, public_dns) -> None:
        body = Counter(chunks=20)  # 20 MB, no Content-Length
        route("/huge").mock(
            return_value=httpx.Response(200, headers={"content-type": "text/plain"}, content=body)
        )
        r = reader_mod.UrlReader(max_bytes=2 * MB)
        with pytest.raises(ValueError, match="too large"):
            await r.read("https://big.example/huge")
        # The point of the fix: it stopped pulling once past the cap. 0.5.0
        # pulled all 20 chunks into memory before measuring.
        assert body.pulled <= 3, f"read {body.pulled} MB to enforce a 2 MB cap"

    @pytest.mark.asyncio
    @respx.mock
    async def test_declared_length_over_cap_is_refused_unread(self, public_dns) -> None:
        body = Counter(chunks=3)
        route("/declared").mock(
            return_value=httpx.Response(
                200,
                headers={"content-type": "text/html", "content-length": str(3 * MB)},
                content=body,
            )
        )
        r = reader_mod.UrlReader(max_bytes=MB)
        with pytest.raises(ValueError, match="too large"):
            await r.read("https://big.example/declared")
        assert body.pulled == 0

    @pytest.mark.asyncio
    @respx.mock
    async def test_too_large_carries_a_code(self, public_dns) -> None:
        route("/huge").mock(
            return_value=httpx.Response(200, headers={"content-type": "text/plain"},
                                        content=Counter(chunks=3))
        )
        r = reader_mod.UrlReader(max_bytes=MB)
        with pytest.raises(reader_mod.ReadError) as exc:
            await r.read("https://big.example/huge")
        assert exc.value.code == "TOO_LARGE"


class TestDeadline:
    @pytest.mark.asyncio
    @respx.mock
    async def test_slow_drip_hits_the_wall_clock_deadline(self, public_dns) -> None:
        # Each chunk arrives well inside any per-read timeout; the total does not.
        route("/drip").mock(
            return_value=httpx.Response(
                200, headers={"content-type": "text/plain"},
                content=Counter(chunks=100, size=10, delay=0.05),
            )
        )
        r = reader_mod.UrlReader(deadline=0.3)
        started = time.monotonic()
        with pytest.raises(TimeoutError):
            await r.read("https://big.example/drip")
        assert time.monotonic() - started < 2.0


class TestContentGate:
    @pytest.mark.asyncio
    @respx.mock
    async def test_pdf_is_refused_before_download(self, public_dns) -> None:
        body = Counter(chunks=2)
        route("/paper.pdf").mock(
            return_value=httpx.Response(200, headers={"content-type": "application/pdf"},
                                        content=body)
        )
        r = reader_mod.UrlReader()
        with pytest.raises(reader_mod.ReadError) as exc:
            await r.read("https://big.example/paper.pdf")
        assert exc.value.code == "UNSUPPORTED_CONTENT_TYPE"
        assert "PDF" in str(exc.value)
        assert body.pulled == 0

    @pytest.mark.asyncio
    @respx.mock
    async def test_unlabelled_binary_is_refused(self, public_dns) -> None:
        route("/blob").mock(return_value=httpx.Response(200, content=b"\x89PNG\x00\x00\x00binary"))
        r = reader_mod.UrlReader()
        with pytest.raises(ValueError):
            await r.read("https://big.example/blob")

    @pytest.mark.asyncio
    @respx.mock
    async def test_json_still_comes_back_raw(self, public_dns) -> None:
        """ridemap reads Nominatim and Overpass JSON through read_url. Keep it."""
        route("/api", host="nominatim.example").mock(
            return_value=httpx.Response(
                200, headers={"content-type": "application/json; charset=utf-8"},
                content=b'[{"lat": "42.44", "display_name": "Ithaca, NY"}]',
            )
        )
        data = await reader_mod.UrlReader().read("https://nominatim.example/api")
        assert data["extraction"] == "raw"
        assert '"display_name": "Ithaca, NY"' in data["markdown"]

    @pytest.mark.asyncio
    @pytest.mark.parametrize("ctype", ["application/rss+xml", "application/osm3s+xml", "text/xml"])
    @respx.mock
    async def test_non_html_xml_comes_back_raw(self, public_dns, ctype) -> None:
        """RSS and Overpass XML used to go through the article extractor and come
        back as NO_CONTENT. Only HTML and XHTML are extracted now."""
        body = b'<?xml version="1.0"?><osm><node id="1" lat="42.44" lon="-76.50"/></osm>'
        route("/xml").mock(return_value=httpx.Response(200, headers={"content-type": ctype},
                                                       content=body))
        data = await reader_mod.UrlReader().read("https://big.example/xml")
        assert data["extraction"] == "raw"
        assert 'lat="42.44"' in data["markdown"]

    @pytest.mark.asyncio
    @respx.mock
    async def test_declared_charset_is_honoured(self, public_dns) -> None:
        route("/latin1").mock(
            return_value=httpx.Response(
                200, headers={"content-type": "text/plain; charset=iso-8859-1"},
                content="café".encode("iso-8859-1"),
            )
        )
        data = await reader_mod.UrlReader().read("https://big.example/latin1")
        assert data["markdown"] == "café"


class TestGuardDoesNotBlockTheLoop:
    """The ticker's count is read at the moment the lookup RETURNS.

    An earlier version read it after gather() finished, by which time the ticker
    had always completed, so a blocking lookup passed too (a reviewer's mutation
    survived). Snapshotting at return is what makes a blocked loop visible: the
    ticker cannot have advanced while the loop thread was stuck in getaddrinfo.
    """

    @staticmethod
    def _slow_dns(monkeypatch, seconds: float = 0.4) -> None:
        def slow_getaddrinfo(host, port, *args, **kwargs):
            time.sleep(seconds)  # a resolver taking its time, as they do
            return [(2, 1, 6, "", (PUBLIC_IP, 0))]

        monkeypatch.setattr(ssrf.socket, "getaddrinfo", slow_getaddrinfo)

    @staticmethod
    async def _ticks_while(coro) -> int:
        ticks = 0

        async def ticker():
            nonlocal ticks
            for _ in range(60):
                await asyncio.sleep(0.01)
                ticks += 1

        async def run_and_snapshot():
            await coro
            return ticks

        tick_task = asyncio.ensure_future(ticker())
        await asyncio.sleep(0)  # let the ticker start first
        seen = await run_and_snapshot()
        tick_task.cancel()
        return seen

    @pytest.mark.asyncio
    async def test_slow_dns_lets_other_tasks_run(self, monkeypatch) -> None:
        self._slow_dns(monkeypatch)
        resolve = getattr(ssrf, "resolve_url_async", None)
        assert resolve is not None, "no async resolver: the lookup runs on the event loop"
        seen = await self._ticks_while(resolve("https://slow-dns.example/"))
        assert seen >= 10, f"event loop stalled during DNS (ticks={seen})"

    @pytest.mark.asyncio
    @respx.mock
    async def test_reader_does_not_block_the_loop_on_dns(self, monkeypatch) -> None:
        """Through UrlReader.read, so the reader cannot quietly go back to the
        blocking resolve_url while the async one stays tested in isolation."""
        self._slow_dns(monkeypatch)
        route("/page", host="slow-dns.example").mock(
            return_value=httpx.Response(200, headers={"content-type": "text/plain"}, content=b"ok")
        )
        seen = await self._ticks_while(
            reader_mod.UrlReader().read("https://slow-dns.example/page")
        )
        assert seen >= 10, f"event loop stalled during the reader's DNS lookup (ticks={seen})"

    @pytest.mark.asyncio
    async def test_async_resolver_rejects_private_answers(self, monkeypatch) -> None:
        monkeypatch.setattr(
            ssrf.socket, "getaddrinfo",
            lambda *a, **k: [(2, 1, 6, "", ("10.0.0.5", 0))],
        )
        with pytest.raises(ssrf.SsrfError):
            await ssrf.resolve_url_async("https://inside.example/")
