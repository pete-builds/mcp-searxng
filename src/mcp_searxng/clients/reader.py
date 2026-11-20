"""URL content reader.

Fetches a URL and extracts the main article content as Markdown using
trafilatura, which strips boilerplate (nav, ads, footers) before conversion.
Anything that is not HTML (plain text, JSON, CSV, RSS and other XML) is
returned as-is. HTML with no extractable main content is an error, not a raw
dump of the page.
Binary bodies (PDF, images, archives) are refused before they are downloaded.
"""

import asyncio
import logging

import httpx
import trafilatura

from mcp_searxng import __version__
from mcp_searxng.clients.ssrf import resolve_url_async

logger = logging.getLogger("searxng.reader")

DEFAULT_TIMEOUT = 20
#: Wall-clock budget for one read: every redirect hop plus the body. httpx's
#: timeout is per phase and per socket read, so on its own a server that sends
#: one byte every 19 seconds keeps a fetch alive indefinitely.
DEFAULT_DEADLINE = 45
DEFAULT_MAX_BYTES = 5_000_000  # 5 MB


_REDIRECT_STATUSES = frozenset({301, 302, 303, 307, 308})
MAX_REDIRECTS = 5

#: Content types read_url returns. Everything else is refused before the body
#: is read: decoding a PDF or an image as text used to hand the agent up to
#: 5 MB of mojibake labelled as markdown.
_TEXT_TYPES = ("text/", "html", "xml", "json", "javascript", "csv", "yaml", "markdown")


class ReadError(ValueError):
    """A read that failed for a reason the caller can act on.

    ``code`` is a stable machine-readable reason; the message says what to do.
    Subclasses ValueError so callers that caught ValueError before still do.
    """

    def __init__(self, code: str, message: str):
        super().__init__(message)
        self.code = code


def _is_text(content_type: str) -> bool:
    ct = content_type.split(";", 1)[0].strip().lower()
    if not ct:
        return True  # unlabelled: decided by sniffing the body instead
    return any(t in ct for t in _TEXT_TYPES)


class UrlReader:
    """Fetch a URL and return clean markdown."""

    def __init__(
        self,
        timeout: int = DEFAULT_TIMEOUT,
        max_bytes: int = DEFAULT_MAX_BYTES,
        deadline: float = DEFAULT_DEADLINE,
        user_agent: str = f"mcp-searxng/{__version__} (+https://github.com/pete-builds/mcp-searxng)",
    ):
        # follow_redirects is deliberately OFF. validate_url runs once, on the URL
        # the caller supplied, so httpx following a 302 for us meant the guard
        # covered only the first hop: an attacker-controlled host that passes
        # validation could answer 302 to 127.0.0.1 or a LAN address and the
        # internal response came straight back to the agent. Redirects are walked
        # manually below so every hop is validated. mcp-cloakroom reached the same
        # conclusion for the same reason -- "a redirect target is a URL we did not
        # vet" -- and simply refuses to follow at all.
        self._client = httpx.AsyncClient(
            timeout=timeout,
            follow_redirects=False,
            headers={"User-Agent": user_agent},
        )
        self._max_bytes = max_bytes
        self._deadline = deadline

    async def _open_pinned(self, url: str) -> httpx.Response:
        """Open a streamed GET of ``url`` at the address the guard vetted.

        The guard resolves the hostname once. Handing the hostname back to httpx
        would resolve it a second time, and nothing forces that answer to match:
        a hostile authoritative server can say "public" to the guard and
        "127.0.0.1" to httpx (DNS rebinding). So the request goes to the vetted
        address as a literal, with the original ``Host`` header so the origin
        serves the right site and ``sni_hostname`` so TLS still verifies the
        certificate against the real name rather than the IP.

        The response is opened in streaming mode: headers only, body unread.
        The caller must close it.
        """
        target = await resolve_url_async(url)
        original = httpx.URL(url)
        pinned = original.copy_with(host=target.address)
        extensions = {"sni_hostname": target.host} if original.scheme == "https" else {}
        request = self._client.build_request(
            "GET",
            pinned,
            headers={"Host": original.netloc.decode("ascii")},
            extensions=extensions,
        )
        return await self._client.send(request, stream=True)

    async def _open_validating_every_hop(self, url: str) -> tuple[httpx.Response, str]:
        """Open ``url``, re-running the SSRF guard on each redirect target.

        Validating only the caller-supplied URL is not enough. The guard resolves
        the hostname and rejects private addresses, which is exactly the check a
        redirect is designed to slip past: the first host is public and passes,
        and the Location header then points inward. Every hop gets the same
        treatment, and the chain is bounded so a redirect loop cannot spin.

        Returns the final response (streamed, body unread, caller closes it) and
        the URL it came from, in the hostname form the chain produced (the
        response's own URL carries the pinned address, which is an
        implementation detail callers must not see).
        """
        current = url
        for _ in range(MAX_REDIRECTS + 1):
            resp = await self._open_pinned(current)
            location = resp.headers.get("location")
            if resp.status_code not in _REDIRECT_STATUSES or not location:
                return resp, current
            # A redirect body is never read: close it before the next hop.
            await resp.aclose()
            # Relative Locations are legal; resolve against the URL we just fetched
            # so the next guard call sees a real absolute target. Join against the
            # hostname form, not resp.url, or a relative Location would inherit the
            # pinned address and skip the name-based check on the next hop.
            current = str(httpx.URL(current).join(location))
        raise ReadError(
            "TOO_MANY_REDIRECTS",
            f"too many redirects (limit {MAX_REDIRECTS}) starting from {url}",
        )

    async def _read_capped(self, resp: httpx.Response) -> bytes:
        """Read the body, stopping as soon as it passes ``max_bytes``.

        The body used to be downloaded in full and measured afterwards, so the
        5 MB limit bounded nothing: a multi-gigabyte response was held in memory
        first, in the one process that serves every session.
        """
        declared = resp.headers.get("content-length")
        if declared and declared.isdigit() and int(declared) > self._max_bytes:
            raise ReadError(
                "TOO_LARGE",
                f"response too large: {declared} bytes declared (limit {self._max_bytes})",
            )
        chunks: list[bytes] = []
        size = 0
        async for chunk in resp.aiter_bytes():
            size += len(chunk)
            if size > self._max_bytes:
                raise ReadError(
                    "TOO_LARGE",
                    f"response too large: over {self._max_bytes} bytes (limit {self._max_bytes})",
                )
            chunks.append(chunk)
        return b"".join(chunks)

    async def read(self, url: str) -> dict:
        """Fetch URL and return {url, title, markdown, length, fetched_status, extraction}.

        Raises SsrfError if the URL, or any redirect hop, targets a
        private/loopback/link-local/reserved address or a non-http(s) scheme;
        ReadError (a ValueError) with a ``code`` for a body that is too large,
        binary, empty of main content, or behind too many redirects;
        httpx.HTTPStatusError for a 4xx/5xx; TimeoutError when the whole read
        exceeds its deadline; and other httpx errors on network failure.
        """
        async with asyncio.timeout(self._deadline):
            resp, final_url = await self._open_validating_every_hop(url)
            try:
                resp.raise_for_status()
                content_type = resp.headers.get("content-type", "")
                if not _is_text(content_type):
                    ct = content_type.split(";", 1)[0].strip()
                    raise ReadError(
                        "UNSUPPORTED_CONTENT_TYPE",
                        f"{ct} is not text; read_url only returns HTML, XML, JSON and "
                        f"plain text. It does not read PDFs, images or other binary files.",
                    )
                body = await self._read_capped(resp)
            finally:
                await resp.aclose()

        if b"\x00" in body[:4096]:
            raise ReadError(
                "UNSUPPORTED_CONTENT_TYPE",
                "body looks binary (NUL bytes) although it was not labelled as such; "
                "read_url only returns text.",
            )
        # Same decoding resp.text applies: the declared charset, else UTF-8.
        text = body.decode(resp.charset_encoding or "utf-8", errors="replace")

        if "html" not in content_type:
            # Plain text, JSON, CSV and non-HTML XML (RSS, Atom, OSM) -- as-is.
            # Only HTML and XHTML go to trafilatura: it extracts article bodies,
            # so an RSS feed or an Overpass XML answer used to come back as
            # NO_CONTENT instead of its own text.
            return {
                "url": final_url,
                "title": None,
                "markdown": text,
                "length": len(text),
                "fetched_status": resp.status_code,
                "extraction": "raw",
            }

        markdown = trafilatura.extract(
            text,
            output_format="markdown",
            include_links=True,
            include_tables=True,
            with_metadata=False,
            favor_recall=False,
        )
        title = None
        meta = trafilatura.extract_metadata(text)
        if meta is not None:
            title = meta.title

        if not markdown:
            raise ReadError(
                "NO_CONTENT",
                "no extractable main content (the page may be a login wall, a "
                "JavaScript app, or a listing with no article body)",
            )

        return {
            "url": final_url,
            "title": title,
            "markdown": markdown,
            "length": len(markdown),
            "fetched_status": resp.status_code,
            "extraction": "trafilatura",
        }
