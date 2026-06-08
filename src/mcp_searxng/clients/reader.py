"""URL content reader.

Fetches a URL and extracts the main article content as Markdown using
trafilatura, which strips boilerplate (nav, ads, footers) before conversion.
Falls back to raw HTML→markdown if trafilatura can't extract a main article.
"""

import logging

import httpx
import trafilatura

from mcp_searxng.clients.ssrf import validate_url

logger = logging.getLogger("searxng.reader")

DEFAULT_TIMEOUT = 20
DEFAULT_MAX_BYTES = 5_000_000  # 5 MB


class UrlReader:
    """Fetch a URL and return clean markdown."""

    def __init__(
        self,
        timeout: int = DEFAULT_TIMEOUT,
        max_bytes: int = DEFAULT_MAX_BYTES,
        user_agent: str = "mcp-searxng/0.2 (+https://github.com/pete-builds/mcp-searxng)",
    ):
        self._client = httpx.AsyncClient(
            timeout=timeout,
            follow_redirects=True,
            headers={"User-Agent": user_agent},
        )
        self._max_bytes = max_bytes

    async def read(self, url: str) -> dict:
        """Fetch URL and return {url, title, markdown, length, fetched_status}.

        Raises httpx errors on network failure, ValueError if the response is too
        large or has no extractable content, and SsrfError if the URL targets a
        private/loopback/link-local/reserved address or a non-http(s) scheme.
        """
        # SSRF guard: reject non-http(s) schemes and any URL whose host resolves
        # to a private/loopback/link-local/reserved/multicast address before we
        # ever open a connection.
        validate_url(url)

        resp = await self._client.get(url)
        resp.raise_for_status()

        if len(resp.content) > self._max_bytes:
            raise ValueError(
                f"response too large: {len(resp.content)} bytes (limit {self._max_bytes})"
            )

        content_type = resp.headers.get("content-type", "")
        if "html" not in content_type and "xml" not in content_type:
            # Plain text / JSON / etc — return as-is
            text = resp.text
            return {
                "url": str(resp.url),
                "title": None,
                "markdown": text,
                "length": len(text),
                "fetched_status": resp.status_code,
                "extraction": "raw",
            }

        html = resp.text
        markdown = trafilatura.extract(
            html,
            output_format="markdown",
            include_links=True,
            include_tables=True,
            with_metadata=False,
            favor_recall=False,
        )
        title = None
        meta = trafilatura.extract_metadata(html)
        if meta is not None:
            title = meta.title

        if not markdown:
            raise ValueError("no extractable main content")

        return {
            "url": str(resp.url),
            "title": title,
            "markdown": markdown,
            "length": len(markdown),
            "fetched_status": resp.status_code,
            "extraction": "trafilatura",
        }
