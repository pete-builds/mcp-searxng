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

logger = logging.getLogger("searxng.queries")

# Minimum seconds between requests to avoid engine rate limits
REQUEST_DELAY = 1.0

# Default in-memory cache TTL (seconds) for identical SearXNG queries
DEFAULT_CACHE_TTL = 300
DEFAULT_CACHE_MAXSIZE = 256


class SearxngClient:
    """Async SearXNG API client with rate limiting, dedup, and a TTL cache."""

    def __init__(
        self,
        url: str,
        cache_ttl: int = DEFAULT_CACHE_TTL,
        cache_maxsize: int = DEFAULT_CACHE_MAXSIZE,
    ):
        self.base_url = url.rstrip("/")
        self._client = httpx.AsyncClient(
            timeout=30,
            limits=httpx.Limits(max_connections=10, max_keepalive_connections=5),
        )
        self._last_request_time = 0.0
        self._cache_ttl = cache_ttl
        self._cache_maxsize = cache_maxsize
        # key -> (expires_at, value)
        self._cache: dict[tuple, tuple[float, dict]] = {}

    async def _rate_limit(self):
        """Enforce minimum delay between requests."""
        now = time.monotonic()
        elapsed = now - self._last_request_time
        if elapsed < REQUEST_DELAY:
            await asyncio.sleep(REQUEST_DELAY - elapsed)
        self._last_request_time = time.monotonic()

    @staticmethod
    def _cache_key(path: str, params: dict | None) -> tuple:
        """Stable cache key from path + sorted params."""
        if not params:
            return (path,)
        return (path, tuple(sorted((str(k), str(v)) for k, v in params.items())))

    def _cache_get(self, key: tuple) -> dict | None:
        entry = self._cache.get(key)
        if entry is None:
            return None
        expires_at, value = entry
        if time.monotonic() >= expires_at:
            self._cache.pop(key, None)
            return None
        return value

    def _cache_put(self, key: tuple, value: dict) -> None:
        if len(self._cache) >= self._cache_maxsize:
            # Evict oldest by expiry. Cheap O(n) — fine at maxsize=256.
            oldest_key = min(self._cache, key=lambda k: self._cache[k][0])
            self._cache.pop(oldest_key, None)
        self._cache[key] = (time.monotonic() + self._cache_ttl, value)

    async def _get(self, path: str, params: dict | None = None) -> dict:
        """GET with TTL cache, rate limiting, and one retry on connection errors."""
        key = self._cache_key(path, params)
        cached = self._cache_get(key)
        if cached is not None:
            return cached

        await self._rate_limit()
        url = f"{self.base_url}{path}"
        for attempt in range(2):
            try:
                resp = await self._client.get(url, params=params)
                resp.raise_for_status()
                data = resp.json()
                self._cache_put(key, data)
                return data
            except (httpx.RemoteProtocolError, httpx.ConnectError):
                if attempt == 0:
                    continue
                raise

    def _shape_results(self, data: dict) -> list[dict]:
        """Extract useful fields from raw SearXNG results."""
        results = []
        for r in data.get("results", []):
            # SearXNG merges cross-engine hits itself and reports `engines`.
            # Keep it: it is the only corroboration signal in the payload.
            engines = r.get("engines") or ([r["engine"]] if r.get("engine") else [])
            result = {
                "title": r.get("title"),
                "url": r.get("url"),
                "content": r.get("content"),
                "engine": r.get("engine"),
                "engines": sorted(set(engines)),
                "engine_count": len(set(engines)),
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
    def _provenance(data: dict, shaped: list[dict]) -> dict:
        """Summarise which engines produced this result set, and flag single-source.

        Reads provenance metadata only, never result content, because search
        results are untrusted input.

        On 2026-08-27 every general engine but bing was CAPTCHA-blocked. Bing
        returns unrelated filler rather than an empty set for terms it has no
        index for, and SearXNG still reported unresponsive_engines=[], so a junk
        result set looked exactly like a healthy one and reached a research
        report. `degraded` plus `warning` is what makes those two distinguishable.
        """
        contributing: set[str] = set()
        corroborated = 0
        single = 0
        for r in shaped:
            engines = r.get("engines") or []
            contributing.update(engines)
            if len(engines) >= 2:
                corroborated += 1
            elif len(engines) == 1:
                single += 1

        unresponsive = data.get("unresponsive_engines") or []
        engine_count = len(contributing)
        degraded = engine_count < 2

        warning = None
        if degraded:
            if not shaped:
                warning = (
                    "DEGRADED: no engine returned any result. "
                    "%d engine(s) were unresponsive. This is not evidence of absence."
                ) % len(unresponsive)
            else:
                warning = (
                    "DEGRADED: all %d results came from a single engine (%s) with no "
                    "cross-engine corroboration, and %d engine(s) were unresponsive. "
                    "A single engine returns unrelated filler rather than an empty set "
                    "for terms it has no index for, so these results may be irrelevant "
                    "even though the query succeeded. Treat them as low confidence and "
                    "verify before citing."
                ) % (len(shaped), ", ".join(sorted(contributing)) or "unknown", len(unresponsive))

        return {
            "engine_count": engine_count,
            "engines_contributing": sorted(contributing),
            "corroborated_results": corroborated,
            "single_engine_results": single,
            "unresponsive_engines": unresponsive,
            "degraded": degraded,
            "warning": warning,
        }

    @staticmethod
    def deduplicate(results: list[dict]) -> list[dict]:
        """Deduplicate results by URL. Boost score for results found by multiple engines."""
        seen = {}
        for r in results:
            url = r.get("url", "")
            if not url:
                continue
            incoming = r.get("engines") or ([r["engine"]] if r.get("engine") else ["?"])
            if url in seen:
                existing = seen[url]
                merged = sorted(set(existing["engines"]) | set(incoming))
                existing["engines"] = merged
                existing["engine_count"] = len(merged)
                existing["score"] = existing.get("score", 0) + r.get("score", 0)
            else:
                seen[url] = {**r, "engines": sorted(set(incoming)),
                             "engine_count": len(set(incoming))}
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
            "provenance": self._provenance(data, results),
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
        deep_unresponsive: list = []

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
            for entry in (data.get("provenance") or {}).get("unresponsive_engines") or []:
                if entry not in deep_unresponsive:
                    deep_unresponsive.append(entry)
            if not page_results:
                break  # No more results
            all_results.extend(page_results)

        deduped = self.deduplicate(all_results)

        logger.info(
            "deep_search query=%r  pages=%d  raw=%d  deduped=%d",
            query, pages, len(all_results), len(deduped),
        )

        # Aggregate unresponsive engines across pages; each page reports its own.
        return {
            "query": query,
            "number_of_results": len(deduped),
            "pages_fetched": pages,
            "provenance": self._provenance({"unresponsive_engines": deep_unresponsive}, deduped),
            "results": deduped,
        }

    async def search_person(
        self,
        name: str,
        location: str = "",
        context: str = "",
    ) -> dict:
        """Fan out multiple targeted searches for a person and merge results.

        Runs targeted searches across: general web, LinkedIn, business filings,
        court/legal, news, social media, and property records. Deduplicates
        across all results. Calls are sequential to respect the SearXNG
        rate limiter.

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
            except Exception as e:
                logger.warning("person_search %s query failed: %s", label, e)
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
