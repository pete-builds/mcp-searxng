"""SearXNG API client.

SearXNG exposes a JSON API at /search with format=json.
No authentication required (self-hosted instance).

Key endpoints:
- GET /search?q=<query>&format=json  — main search
- GET /config                        — instance configuration and enabled engines
- GET /stats                         — query statistics (if enabled in settings)
"""

import asyncio
import logging
import time

import httpx

from exceptions import SearxngAPIError, SearxngConnectionError, SearxngError, SearxngParseError

logger = logging.getLogger("searxng.queries")

# Minimum seconds between requests to avoid engine rate limits
REQUEST_DELAY = 1.0


class SearxngClient:
    """Async SearXNG API client with rate limiting and deduplication."""

    def __init__(self, url: str):
        self.base_url = url.rstrip("/")
        self._client = httpx.AsyncClient(
            timeout=30,
            limits=httpx.Limits(max_connections=10, max_keepalive_connections=5),
        )
        self._last_request_time = 0.0

    async def _rate_limit(self):
        """Enforce minimum delay between requests."""
        now = time.monotonic()
        elapsed = now - self._last_request_time
        if elapsed < REQUEST_DELAY:
            await asyncio.sleep(REQUEST_DELAY - elapsed)
        self._last_request_time = time.monotonic()

    async def _get(self, path: str, params: dict | None = None) -> dict:
        """GET with rate limiting and one retry on connection errors."""
        await self._rate_limit()
        url = f"{self.base_url}{path}"
        for attempt in range(2):
            try:
                resp = await self._client.get(url, params=params)
                resp.raise_for_status()
                try:
                    return resp.json()
                except (ValueError, TypeError) as exc:
                    raise SearxngParseError(url, f"invalid JSON: {exc}") from exc
            except httpx.HTTPStatusError as exc:
                raise SearxngAPIError(exc.response.status_code, url, str(exc)) from exc
            except (httpx.RemoteProtocolError, httpx.ConnectError) as exc:
                if attempt == 0:
                    logger.debug("Retrying %s after connection error: %s", url, exc)
                    continue
                raise SearxngConnectionError(url, str(exc)) from exc
            except httpx.TimeoutException as exc:
                raise SearxngConnectionError(url, f"timeout: {exc}") from exc

    def _shape_results(self, data: dict) -> list[dict]:
        """Extract useful fields from raw SearXNG results."""
        results = []
        for r in data.get("results", []):
            result = {
                "title": r.get("title"),
                "url": r.get("url"),
                "content": r.get("content"),
                "engine": r.get("engine"),
            }
            if r.get("publishedDate"):
                result["published_date"] = r["publishedDate"]
            if r.get("score"):
                result["score"] = r["score"]
            if r.get("thumbnail"):
                result["thumbnail"] = r["thumbnail"]
            results.append(result)
        return results

    @staticmethod
    def deduplicate(results: list[dict]) -> list[dict]:
        """Deduplicate results by URL. Boost score for results found by multiple engines."""
        seen = {}
        for r in results:
            url = r.get("url", "")
            if not url:
                continue
            if url in seen:
                # Track additional engines that found this result
                existing = seen[url]
                existing_engines = existing.get("engines", [existing.get("engine", "?")])
                new_engine = r.get("engine", "?")
                if new_engine not in existing_engines:
                    existing_engines.append(new_engine)
                existing["engines"] = existing_engines
                existing["engine_count"] = len(existing_engines)
                # Boost score for multi-engine results
                existing["score"] = existing.get("score", 0) + r.get("score", 0)
            else:
                seen[url] = {**r, "engines": [r.get("engine", "?")], "engine_count": 1}
        # Sort by engine_count (multi-engine first), then score
        deduped = sorted(seen.values(), key=lambda x: (x["engine_count"], x.get("score", 0)), reverse=True)
        return deduped

    async def search(
        self,
        query: str,
        categories: str = "general",
        engines: str = "",
        language: str = "en",
        page: int = 1,
        time_range: str = "",
        safesearch: int = 0,
    ) -> dict:
        """Execute a search query.

        Args:
            query: Search query string.
            categories: Comma-separated categories.
            engines: Comma-separated engine names (optional, overrides categories).
            language: Language code (default: en).
            page: Results page number (default: 1).
            time_range: Filter by time: day, week, month, year (optional).
            safesearch: 0=off, 1=moderate, 2=strict.

        Returns:
            Dict with results, suggestions, and metadata.
        """
        params: dict = {
            "q": query,
            "format": "json",
            "categories": categories,
            "language": language,
            "pageno": page,
            "safesearch": safesearch,
        }
        if engines:
            params["engines"] = engines
        if time_range:
            params["time_range"] = time_range

        data = await self._get("/search", params=params)
        results = self._shape_results(data)

        # Log the query
        engines_used = sorted(set(r.get("engine", "?") for r in results))
        logger.info(
            "query=%r  categories=%s  results=%d  engines=%s  time_range=%s",
            query, categories, len(results), ",".join(engines_used), time_range or "none",
        )

        return {
            "query": data.get("query"),
            "number_of_results": data.get("number_of_results", len(results)),
            "results": results,
            "suggestions": data.get("suggestions", []),
            "corrections": data.get("corrections", []),
            "infoboxes": [
                {
                    "title": ib.get("infobox"),
                    "content": ib.get("content"),
                    "urls": ib.get("urls", []),
                }
                for ib in data.get("infoboxes", [])
            ],
        }

    async def search_deep(
        self,
        query: str,
        categories: str = "general",
        engines: str = "",
        language: str = "en",
        pages: int = 3,
        time_range: str = "",
    ) -> dict:
        """Search across multiple result pages and deduplicate.

        Args:
            query: Search query string.
            categories: Comma-separated categories.
            engines: Comma-separated engine names (optional).
            language: Language code (default: en).
            pages: Number of pages to fetch (default: 3, max: 5).
            time_range: Filter by time (optional).

        Returns:
            Dict with deduplicated results from all pages.
        """
        pages = min(pages, 5)
        all_results = []

        for page_num in range(1, pages + 1):
            data = await self.search(
                query=query,
                categories=categories,
                engines=engines,
                language=language,
                page=page_num,
                time_range=time_range,
            )
            page_results = data.get("results", [])
            if not page_results:
                break  # No more results
            all_results.extend(page_results)

        deduped = self.deduplicate(all_results)

        logger.info(
            "deep_search query=%r  pages=%d  raw=%d  deduped=%d",
            query, pages, len(all_results), len(deduped),
        )

        return {
            "query": query,
            "number_of_results": len(deduped),
            "pages_fetched": pages,
            "results": deduped,
        }

    async def search_person(
        self,
        name: str,
        location: str = "",
        context: str = "",
    ) -> dict:
        """Fan out multiple targeted searches for a person and merge results.

        Runs parallel searches across: general web, LinkedIn, business filings,
        court/legal, news, social media, and property records. Deduplicates
        across all results.

        Args:
            name: Full name of the person (will be quoted in searches).
            location: City, state, or region (optional but improves accuracy).
            context: Additional context like employer, business, or role (optional).

        Returns:
            Dict with categorized, deduplicated results from all search angles.
        """
        quoted = f'"{name}"'
        loc = f" {location}" if location else ""
        ctx = f" {context}" if context else ""

        # Define search queries by category
        queries = {
            "identity": f"{quoted}{loc}",
            "professional": f"site:linkedin.com {quoted}{loc}",
            "business": f"{quoted} LLC OR Inc OR Corp{loc}",
            "legal": f"{quoted} court OR lawsuit OR plaintiff OR defendant{loc}",
            "news": f"{quoted}{loc}{ctx}",
            "social": f"{quoted} site:facebook.com OR site:instagram.com OR site:twitter.com",
            "property": f"{quoted} property OR real estate{loc}",
            "reddit": f"{quoted}{loc} site:reddit.com",
        }

        # Category to SearXNG category mapping
        category_map = {
            "identity": "general",
            "professional": "general",
            "business": "general",
            "legal": "general",
            "news": "news",
            "social": "general",
            "property": "general",
            "reddit": "general",
        }

        all_results = {}
        total_raw = 0

        for label, query in queries.items():
            try:
                data = await self.search(
                    query=query,
                    categories=category_map[label],
                )
                results = data.get("results", [])[:20]
                # Tag each result with the search category
                for r in results:
                    r["search_category"] = label
                all_results[label] = results
                total_raw += len(results)
            except SearxngError as exc:
                logger.warning("person_search %s query failed: %s", label, exc)
                all_results[label] = []

        # Also merge all results for a deduplicated master list
        flat = []
        for results in all_results.values():
            flat.extend(results)
        deduped = self.deduplicate(flat)

        logger.info(
            "person_search name=%r  location=%r  raw=%d  deduped=%d",
            name, location, total_raw, len(deduped),
        )

        return {
            "name": name,
            "location": location,
            "context": context,
            "total_results": len(deduped),
            "by_category": {k: len(v) for k, v in all_results.items()},
            "results": deduped,
            "categorized": {k: v for k, v in all_results.items() if v},
        }

    async def get_config(self) -> dict:
        """Get SearXNG instance configuration: enabled engines, categories, plugins."""
        data = await self._get("/config")

        engines = []
        for eng in data.get("engines", []):
            engines.append({
                "name": eng.get("name"),
                "categories": eng.get("categories", []),
                "language_support": eng.get("language_support", False),
                "enabled": eng.get("enabled", True),
            })

        categories = sorted(data.get("categories", []))

        return {
            "instance_name": data.get("instance_name"),
            "version": data.get("version"),
            "engines_count": len(engines),
            "engines": engines,
            "categories": categories,
            "safe_search": data.get("safe_search"),
            "default_locale": data.get("default_locale"),
        }
