"""URL content reader.

Fetches a URL and extracts the main article content as Markdown using
trafilatura, which strips boilerplate (nav, ads, footers) before conversion.
Falls back to raw HTML→markdown if trafilatura can't extract a main article.
"""

import logging

import httpx
import trafilatura

from mcp_searxng.clients.ssrf import resolve_url

logger = logging.getLogger("searxng.reader")

DEFAULT_TIMEOUT = 20
DEFAULT_MAX_BYTES = 5_000_000  # 5 MB


_REDIRECT_STATUSES = frozenset({301, 302, 303, 307, 308})
MAX_REDIRECTS = 5


class UrlReader:
    """Fetch a URL and return clean markdown."""

    def __init__(
        self,
        timeout: int = DEFAULT_TIMEOUT,
        max_bytes: int = DEFAULT_MAX_BYTES,
        user_agent: str = "mcp-searxng/0.2 (+https://github.com/pete-builds/mcp-searxng)",
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

    async def _get_pinned(self, url: str) -> httpx.Response:
        """GET ``url`` at the address the guard vetted, never a fresh lookup.

        The guard resolves the hostname once. Handing the hostname back to httpx
        would resolve it a second time, and nothing forces that answer to match:
        a hostile authoritative server can say "public" to the guard and
        "127.0.0.1" to httpx (DNS rebinding). So the request goes to the vetted
        address as a literal, with the original ``Host`` header so the origin
        serves the right site and ``sni_hostname`` so TLS still verifies the
        certificate against the real name rather than the IP.
        """
        target = resolve_url(url)
        original = httpx.URL(url)
        pinned = original.copy_with(host=target.address)
        extensions = {"sni_hostname": target.host} if original.scheme == "https" else {}
        return await self._client.get(
            pinned,
            headers={"Host": original.netloc.decode("ascii")},
            extensions=extensions,
        )

    async def _fetch_validating_every_hop(self, url: str) -> tuple[httpx.Response, str]:
        """GET ``url``, re-running the SSRF guard on each redirect target.

        Validating only the caller-supplied URL is not enough. The guard resolves
        the hostname and rejects private addresses, which is exactly the check a
        redirect is designed to slip past: the first host is public and passes,
        and the Location header then points inward. Every hop gets the same
        treatment, and the chain is bounded so a redirect loop cannot spin.

        Returns the final response and the URL it came from, in the hostname
        form the chain produced (the response's own URL carries the pinned
        address, which is an implementation detail callers must not see).
        """
        current = url
        for _ in range(MAX_REDIRECTS + 1):
            resp = await self._get_pinned(current)
            if resp.status_code not in _REDIRECT_STATUSES:
                resp.raise_for_status()
                return resp, current
            location = resp.headers.get("location")
            if not location:
                resp.raise_for_status()
                return resp, current
            # Relative Locations are legal; resolve against the URL we just fetched
            # so the next guard call sees a real absolute target. Join against the
            # hostname form, not resp.url, or a relative Location would inherit the
            # pinned address and skip the name-based check on the next hop.
            current = str(httpx.URL(current).join(location))
        raise ValueError(f"too many redirects (limit {MAX_REDIRECTS}) starting from {url}")

    async def read(self, url: str) -> dict:
        """Fetch URL and return {url, title, markdown, length, fetched_status}.

        Raises httpx errors on network failure, ValueError if the response is too
        large, has no extractable content, or exceeds the redirect limit, and
        SsrfError if the URL -- or any redirect hop -- targets a
        private/loopback/link-local/reserved address or a non-http(s) scheme.
        """
        resp, final_url = await self._fetch_validating_every_hop(url)

        if len(resp.content) > self._max_bytes:
            raise ValueError(
                f"response too large: {len(resp.content)} bytes (limit {self._max_bytes})"
            )

        content_type = resp.headers.get("content-type", "")
        if "html" not in content_type and "xml" not in content_type:
            # Plain text / JSON / etc — return as-is
            text = resp.text
            return {
                "url": final_url,
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
            "url": final_url,
            "title": title,
            "markdown": markdown,
            "length": len(markdown),
            "fetched_status": resp.status_code,
            "extraction": "trafilatura",
        }
