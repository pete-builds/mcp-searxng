"""SearXNG API client.

SearXNG exposes a JSON API at /search with format=json.
No authentication required (self-hosted instance).

Key endpoints:
- GET /search?q=<query>&format=json  — main search
- GET /config                        — instance configuration and enabled engines
- GET /stats                         — query statistics (if enabled in settings)
"""

import asyncio
import json
import logging
import time
from urllib.parse import parse_qsl, urlencode, urlsplit

import httpx

logger = logging.getLogger("searxng.queries")

# Minimum seconds between requests to avoid engine rate limits
REQUEST_DELAY = 1.0

# Default in-memory cache TTL (seconds) for identical SearXNG queries
DEFAULT_CACHE_TTL = 300
DEFAULT_CACHE_MAXSIZE = 256

#: search_deep page bounds. More than 5 pages mostly re-fetches the tail of the
#: same few engines, at one rate-limited request per page.
MAX_PAGES = 5

#: search_deep keeps SearXNG's own order for this many results, then ranks the
#: rest by their best per-page score. See rank_deep().
DEEP_HEAD = 10

#: Query parameters that identify a click, not a page.
_TRACKING_PARAMS = frozenset({
    "fbclid", "gclid", "dclid", "msclkid", "mc_cid", "mc_eid", "igshid", "ref_src",
})


class SearxngError(RuntimeError):
    """SearXNG could not answer, with a message that says what to fix.

    The raw httpx error carried the internal SearXNG URL and no hint. The most
    common failure, a 403 because the instance has the JSON API switched off,
    looked like a permissions problem on the agent's side.
    """

    def __init__(self, code: str, message: str):
        super().__init__(message)
        self.code = code


def canonical_url(url: str) -> str:
    """The key two URLs share when they are the same page.

    Ignores the scheme, a leading ``www.``, a default port, a trailing slash,
    the fragment, ``utm_*`` and click-tracking parameters, and query order.
    Used only to decide what is a duplicate; the URL a caller sees is always
    one the engines actually returned.
    """
    p = urlsplit(url)
    host = (p.hostname or "").lower()
    if host.startswith("www."):
        host = host[4:]
    try:
        port = p.port
    except ValueError:
        port = None
    netloc = f"{host}:{port}" if port and port not in (80, 443) else host
    path = p.path.rstrip("/") or "/"
    query = sorted(
        (k, v) for k, v in parse_qsl(p.query, keep_blank_values=True)
        if not k.lower().startswith("utm_") and k.lower() not in _TRACKING_PARAMS
    )
    return netloc + path + (f"?{urlencode(query)}" if query else "")


def _status_error(status: int) -> SearxngError:
    """Map a SearXNG HTTP status to a message that names the fix."""
    if status == 403:
        return SearxngError(
            "SEARXNG_FORBIDDEN",
            "SearXNG refused the request (403). The usual cause is that its JSON "
            "API is disabled: add `json` under `search.formats` in SearXNG's "
            "settings.yml, then restart SearXNG.",
        )
    if status == 429:
        return SearxngError(
            "SEARXNG_RATE_LIMITED",
            "SearXNG rate-limited this server (429). Its bot limiter is probably "
            "on; wait and retry, or exempt this server's address in SearXNG's "
            "limiter settings.",
        )
    return SearxngError(
        "SEARXNG_HTTP_ERROR",
        f"SearXNG answered HTTP {status}. Check the SearXNG logs.",
    )


def parse_domains(value: str) -> list[str]:
    """Comma-separated domains to bare lowercase hostnames.

    Accepts what an agent tends to pass: ``docs.python.org``,
    ``https://docs.python.org/3/``, ``www.example.com``.
    """
    out: list[str] = []
    for raw in (value or "").split(","):
        raw = raw.strip().lower()
        if not raw:
            continue
        host = urlsplit(raw if "//" in raw else f"//{raw}").hostname or ""
        if host.startswith("www."):
            host = host[4:]
        if host and host not in out:
            out.append(host)
    return out


def host_matches(url: str | None, domains: list[str]) -> bool:
    """True when the URL's host is one of ``domains`` or a subdomain of one."""
    host = (urlsplit(url or "").hostname or "").lower()
    if host.startswith("www."):
        host = host[4:]
    return any(host == d or host.endswith("." + d) for d in domains)


PERSON_CAVEAT = (
    "These are search-engine matches for a name, not verified facts about one "
    "person. Common names return pages about different people, and a page can "
    "match on the name alone. `match` reports only whether the result's own "
    "title or snippet contains the name and the location; it is not identity "
    "confirmation. Before attributing a result to the subject, read the page "
    "and confirm at least one independent detail (location, employer, role)."
)


def _person_match(result: dict, name: str, location: str) -> dict:
    """What a result's own title and snippet say about the name and place.

    A string check, nothing more: it separates results that at least mention
    the person from the filler a search returns for loose matches, which the
    old consensus ranking presented as equally strong.
    """
    text = f"{result.get('title') or ''} {result.get('content') or ''}".lower()
    tokens = [t for t in name.lower().replace(".", " ").split() if len(t) > 1]
    loc_tokens = [
        t for t in location.lower().replace(",", " ").split() if len(t) > 2
    ]
    return {
        "name_in_text": bool(tokens) and all(t in text for t in tokens),
        "location_in_text": (any(t in text for t in loc_tokens) if loc_tokens else None),
    }


class SearxngClient:
    """Async SearXNG API client with rate limiting, dedup, and a TTL cache."""

    def __init__(
        self,
        url: str,
        cache_ttl: int = DEFAULT_CACHE_TTL,
        cache_maxsize: int = DEFAULT_CACHE_MAXSIZE,
        min_interval: float | None = None,
    ):
        self.base_url = url.rstrip("/")
        self._client = httpx.AsyncClient(
            timeout=30,
            limits=httpx.Limits(max_connections=10, max_keepalive_connections=5),
        )
        self._last_request_time = 0.0
        # None means the module default, REQUEST_DELAY, read at call time.
        self._min_interval = min_interval
        # Serialises the spacing decision. Without it, N concurrent callers all
        # read the same _last_request_time, all sleep the same amount, and all
        # fire together: measured gaps of 0.0s between 5 concurrent requests.
        self._rate_lock = asyncio.Lock()
        # Identical requests already on the wire: later callers await the first
        # instead of sending a duplicate that would also count against the
        # engines' IP-level rate limits.
        self._inflight: dict[tuple, asyncio.Task] = {}
        self._cache_ttl = cache_ttl
        self._cache_maxsize = cache_maxsize
        # key -> (expires_at, value)
        self._cache: dict[tuple, tuple[float, dict]] = {}

    async def _rate_limit(self):
        """Enforce a minimum delay between requests, across concurrent callers."""
        delay = REQUEST_DELAY if self._min_interval is None else self._min_interval
        if delay <= 0:
            return
        async with self._rate_lock:
            now = time.monotonic()
            elapsed = now - self._last_request_time
            if elapsed < delay:
                await asyncio.sleep(delay - elapsed)
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
        """GET with TTL cache, in-flight coalescing, rate limiting, one retry.

        Raises SearxngError, never a raw httpx error.
        """
        key = self._cache_key(path, params)
        cached = self._cache_get(key)
        if cached is not None:
            return cached

        task = self._inflight.get(key)
        if task is None:
            task = asyncio.ensure_future(self._fetch(path, params, key))
            self._inflight[key] = task
            task.add_done_callback(lambda _done: self._inflight.pop(key, None))
        # shield: one caller cancelling must not cancel the request for the rest.
        return await asyncio.shield(task)

    async def _fetch(self, path: str, params: dict | None, key: tuple) -> dict:
        await self._rate_limit()
        url = f"{self.base_url}{path}"
        for attempt in range(2):
            try:
                resp = await self._client.get(url, params=params)
                resp.raise_for_status()
                data = resp.json()
                self._cache_put(key, data)
                return data
            # ReadError is a connection reset mid-response. Like a refused or
            # dropped connection it is worth one retry: the GET is idempotent.
            except (httpx.RemoteProtocolError, httpx.ConnectError, httpx.ReadError) as e:
                if attempt == 0:
                    continue
                raise SearxngError(
                    "SEARXNG_UNREACHABLE",
                    "Could not connect to SearXNG at the configured SEARXNG_URL "
                    f"({type(e).__name__}). Check that it is running and reachable "
                    "from this server.",
                ) from e
            except httpx.TimeoutException as e:
                raise SearxngError(
                    "SEARXNG_TIMEOUT",
                    "SearXNG did not answer in time. Its engines may be slow or "
                    "blocked; retry, or narrow the query to fewer engines.",
                ) from e
            except httpx.HTTPStatusError as e:
                raise _status_error(e.response.status_code) from e
            except json.JSONDecodeError as e:
                ctype = resp.headers.get("content-type", "unknown")
                raise SearxngError(
                    "SEARXNG_NOT_JSON",
                    f"SearXNG answered with {ctype}, not JSON. Enable the JSON API: "
                    "add `json` under `search.formats` in SearXNG's settings.yml, "
                    "then restart SearXNG.",
                ) from e
            except httpx.HTTPError as e:
                # Everything else httpx can raise (WriteError, LocalProtocolError,
                # DecodingError, ...). Without this, one of them escaped raw and,
                # because search_person catches only SearxngError per angle, a
                # single bad angle failed the whole 8-angle call.
                raise SearxngError(
                    "SEARXNG_REQUEST_FAILED",
                    f"The request to SearXNG failed ({type(e).__name__}). Retry; if it "
                    "persists, check the SearXNG logs.",
                ) from e
        raise AssertionError("unreachable")  # pragma: no cover

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
        """Deduplicate results by canonical URL, keeping SearXNG's own order.

        Two URLs are the same result when :func:`canonical_url` says so, so
        ``http://`` and ``https://``, ``www.``, a trailing slash, a fragment
        and tracking parameters no longer survive as separate entries. The
        URL kept is the first one seen, which is always one an engine returned.

        Order: first sighting wins, so page 1 comes first and each page keeps
        SearXNG's relevance order. search_deep then applies rank_deep().
        """
        seen: dict[str, dict] = {}
        for r in results:
            url = r.get("url", "")
            if not url:
                continue
            key = canonical_url(url)
            incoming = r.get("engines") or ([r["engine"]] if r.get("engine") else ["?"])
            if key in seen:
                existing = seen[key]
                merged = sorted(set(existing["engines"]) | set(incoming))
                existing["engines"] = merged
                existing["engine_count"] = len(merged)
                existing["score"] = existing.get("score", 0) + r.get("score", 0)
            else:
                seen[key] = {**r, "engines": sorted(set(incoming)),
                             "engine_count": len(set(incoming))}
        return list(seen.values())

    @staticmethod
    def rank_deep(merged: list[dict], raw: list[dict], head: int = DEEP_HEAD) -> list[dict]:
        """Order a multi-page merge: SearXNG's order up top, best page score below.

        ``merged`` is deduplicate() output (first-seen order); ``raw`` is every
        result from every page, before merging.

        The first ``head`` results stay in SearXNG's order: that is page 1's
        top, which SearXNG already ranked using every engine. The rest are
        ordered by the best score each result earned on any page. Scores
        restart on every page, so this interleaves the strong top of pages 2+
        with the long tail of page 1 instead of letting a 100-result page 1
        crowd them out of the max_results cut.

        Up to 0.5.0 the whole list was sorted by engine_count, then by score
        summed across pages. bench/ranking_experiment.py measured both on 24
        queries (3 pages): nDCG@10 0.416 -> 0.473 (better on 18, worse on 6),
        Recall@50 0.896 -> 0.896. engine_count is still reported on every
        result; it is no longer the sort key.
        """
        best: dict[str, float] = {}
        for r in raw:
            key = canonical_url(r.get("url") or "")
            best[key] = max(best.get(key, 0.0), r.get("score") or 0.0)
        tail = sorted(
            merged[head:],  # stable: ties keep first-seen order
            key=lambda r: -best.get(canonical_url(r.get("url") or ""), 0.0),
        )
        return merged[:head] + tail

    async def search(
        self,
        query: str,
        categories: str = "general",
        engines: str = "",
        language: str = "en",
        page: int = 1,
        time_range: str = "",
        safesearch: int = 0,
        include_domains: str = "",
        exclude_domains: str = "",
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
            include_domains: Comma-separated domains; keep only results on them
                (subdomains included).
            exclude_domains: Comma-separated domains to drop from the results.

        Returns:
            Dict with results, suggestions, and metadata.
        """
        include = parse_domains(include_domains)
        exclude = parse_domains(exclude_domains)
        q = query
        if include:
            # site: lets the engines that honour it return more on-domain hits.
            # Not every engine does, so the post-filter below is what enforces it.
            q = f"{query} " + " OR ".join(f"site:{d}" for d in include)
        params: dict = {
            "q": q,
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
        filtered_out = 0
        if include or exclude:
            kept = [
                r for r in results
                if (not include or host_matches(r.get("url"), include))
                and not (exclude and host_matches(r.get("url"), exclude))
            ]
            filtered_out = len(results) - len(kept)
            results = kept

        # Log the query
        engines_used = sorted(set(r.get("engine", "?") for r in results))
        # DEBUG, not INFO: queries are personal research (and, from
        # search_person, people's names). They do not belong in default logs.
        logger.debug(
            "query=%r  categories=%s  results=%d  engines=%s  time_range=%s",
            query, categories, len(results), ",".join(engines_used), time_range or "none",
        )

        envelope = {
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
        if include or exclude:
            envelope["domain_filter"] = {
                "include": include, "exclude": exclude, "filtered_out": filtered_out,
            }
        return envelope

    async def search_deep(
        self,
        query: str,
        categories: str = "general",
        engines: str = "",
        language: str = "en",
        pages: int = 3,
        time_range: str = "",
        include_domains: str = "",
        exclude_domains: str = "",
    ) -> dict:
        """Search across multiple result pages and deduplicate.

        Args:
            query: Search query string.
            categories: Comma-separated categories.
            engines: Comma-separated engine names (optional).
            language: Language code (default: en).
            pages: Number of pages to fetch (default: 3, max: 5).
            time_range: Filter by time (optional).
            include_domains: Comma-separated domains to keep (optional).
            exclude_domains: Comma-separated domains to drop (optional).

        Returns:
            Dict with deduplicated results from all pages.
        """
        pages = max(1, min(pages, MAX_PAGES))
        all_results = []
        deep_unresponsive: list = []
        fetched = 0

        for page_num in range(1, pages + 1):
            fetched += 1
            data = await self.search(
                query=query,
                categories=categories,
                engines=engines,
                language=language,
                page=page_num,
                time_range=time_range,
                include_domains=include_domains,
                exclude_domains=exclude_domains,
            )
            page_results = data.get("results", [])
            for entry in (data.get("provenance") or {}).get("unresponsive_engines") or []:
                if entry not in deep_unresponsive:
                    deep_unresponsive.append(entry)
            filtered_out = (data.get("domain_filter") or {}).get("filtered_out", 0)
            if not page_results and not filtered_out:
                break  # SearXNG has no more results; a page emptied by a filter is not that
            all_results.extend(page_results)

        deduped = self.rank_deep(self.deduplicate(all_results), all_results)

        logger.debug(
            "deep_search query=%r  pages=%d  raw=%d  deduped=%d",
            query, fetched, len(all_results), len(deduped),
        )

        # Aggregate unresponsive engines across pages; each page reports its own.
        return {
            "query": query,
            "number_of_results": len(deduped),
            # Pages actually requested. Fewer than pages_requested means a page
            # came back empty and the sweep stopped there. This used to echo
            # the requested count whether or not those pages were fetched.
            "pages_fetched": fetched,
            "pages_requested": pages,
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
            "social": (
                f"{quoted}{loc} site:facebook.com OR site:instagram.com "
                "OR site:x.com OR site:twitter.com"
            ),
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
        failed: dict[str, str] = {}
        last_error: SearxngError | None = None

        for label, query in queries.items():
            try:
                data = await self.search(
                    query=query,
                    categories=category_map[label],
                )
                results = data.get("results", [])[:20]
                # Tag each result with the search category and with what its
                # own title and snippet say, which is all a search can show.
                for r in results:
                    r["search_category"] = label
                    r["match"] = _person_match(r, name, location)
                all_results[label] = results
                total_raw += len(results)
            except SearxngError as e:
                logger.warning("person_search %s angle failed: %s", label, e.code)
                failed[label] = e.code
                last_error = e
                all_results[label] = []

        if last_error is not None and len(failed) == len(queries):
            # Every angle failed: that is a SearXNG problem, not "no results".
            raise last_error

        # Also merge all results for a deduplicated master list
        flat = []
        for results in all_results.values():
            flat.extend(results)
        # Results whose own text names the person (and the place) first; within
        # each group, the order the searches produced them.
        deduped = sorted(
            self.deduplicate(flat),
            key=lambda r: (
                not r["match"]["name_in_text"],
                r["match"]["location_in_text"] is False,
            ),
        )

        logger.debug(
            "person_search name=%r  location=%r  raw=%d  deduped=%d",
            name, location, total_raw, len(deduped),
        )

        return {
            "name": name,
            "location": location,
            "context": context,
            "caveat": PERSON_CAVEAT,
            "total_results": len(deduped),
            "name_matches": sum(1 for r in deduped if r["match"]["name_in_text"]),
            "by_category": {k: len(v) for k, v in all_results.items()},
            "results": deduped,
            "categorized": {k: v for k, v in all_results.items() if v},
            "failed_angles": failed,
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
