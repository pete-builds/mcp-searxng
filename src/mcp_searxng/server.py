"""MCP SearXNG - MCP server for self-hosted SearXNG metasearch.

Provides Claude Code tools for web search, news, deep multi-page search,
person/people-vetting fan-out, image and video search, and URL reading
via the Model Context Protocol (Streamable HTTP transport).

Designed as a grounded search backend for the /research and /vet skills.
"""

import logging
import sys

from dotenv import load_dotenv
from fastmcp import FastMCP
from pete_mcp_core import (
    build_auth_provider,
    configure_logging,
    format_response,
    run_server,
)
from pete_mcp_core.settings import BaseCoreSettings
from pydantic import AliasChoices, Field, ValidationError

from mcp_searxng.clients.reader import UrlReader
from mcp_searxng.clients.searxng import SearxngClient
from mcp_searxng.clients.ssrf import SsrfError

load_dotenv()


class SearxngSettings(BaseCoreSettings):
    searxng_url: str = Field(
        default="",
        validation_alias=AliasChoices("SEARXNG_URL", "MCP_SEARXNG_URL"),
        description="SearXNG base URL (e.g. http://your-searxng-host:8888).",
    )
    searxng_cache_ttl: int = Field(
        default=300,
        validation_alias=AliasChoices("SEARXNG_CACHE_TTL", "MCP_SEARXNG_CACHE_TTL"),
        description="Result cache TTL in seconds.",
    )


try:
    settings = SearxngSettings()
except ValidationError as exc:
    print(f"FATAL: invalid configuration: {exc}", file=sys.stderr)
    sys.exit(1)

if not settings.searxng_url:
    print("FATAL: Missing required environment variable:", file=sys.stderr)
    print("  SEARXNG_URL (SearXNG base URL, e.g. http://your-searxng-host:8888)", file=sys.stderr)
    print("\nCopy .env.example to .env and fill in your values.", file=sys.stderr)
    sys.exit(1)

configure_logging(settings.log_level, settings.log_format)
logger = logging.getLogger("searxng")

searxng = SearxngClient(url=settings.searxng_url, cache_ttl=settings.searxng_cache_ttl)
reader = UrlReader()

mcp = FastMCP(
    "SearXNG Search",
    auth=build_auth_provider(
        settings.auth_token,
        client_id="searxng",
        required=settings.auth_required,
        logger=logger,
    ),
)


# Kept as an alias so the ~12 existing `_format(...)` call sites stay identical.
_format = format_response


# --- Tool annotations ---
# This server is entirely read-only: nine tools, none of which writes anything
# anywhere. That is worth DECLARING rather than leaving to be inferred, because
# an unannotated read-only server and an unannotated server full of delete
# tools are indistinguishable in the manifest. A client trying to be careful
# has to be careful about everything, which in practice means being careful
# about nothing.
#
# Read-only is not the same as harmless, and the annotations do not claim it
# is. read_url fetches an attacker-influenceable page, and the search tools
# return text a caller did not write. That risk is handled by the SSRF
# validation and redirect checks in clients/, not by a hint -- these hints
# describe EFFECTS on the world, and the effect of every tool here is none.

#: Reads only, over the network. Safe to repeat: an answer may differ between
#: two identical calls because the web moved, not because the call changed it.
READ_ONLY = {
    "readOnlyHint": True,
    "destructiveHint": False,
    "idempotentHint": True,
    "openWorldHint": True,
}


# ============================================================
# Person / vetting (the headline tool)
# ============================================================


@mcp.tool(annotations=READ_ONLY)
async def search_person(
    name: str,
    location: str = "",
    context: str = "",
) -> str:
    """Vet a person across 8 angles in a single call.

    Fans out targeted searches for: identity, LinkedIn, business filings,
    court/legal records, news, social media, property records, and Reddit.
    Results are deduplicated and categorized. Built for due-diligence
    workflows; much more efficient than running 8+ separate search calls.

    Args:
        name: Full name of the person (e.g. "John Smith"). Quoted automatically.
        location: City/state/region to narrow results (e.g. "Ithaca NY"). Optional but recommended.
        context: Employer, business name, or role. Optional.

    Returns:
        JSON with categorized results (identity, professional, business, legal,
        news, social, property, reddit), plus a deduplicated master list sorted
        by multi-engine consensus.
    """
    data = await searxng.search_person(name=name, location=location, context=context)
    return _format(data)


# ============================================================
# Search tools
# ============================================================


@mcp.tool(annotations=READ_ONLY)
async def search(
    query: str,
    categories: str = "general",
    engines: str = "",
    language: str = "en",
    max_results: int = 10,
    time_range: str = "",
) -> str:
    """Search the web using SearXNG metasearch. Aggregates results from multiple engines.

    Best for: general web queries, finding documentation, researching topics, looking up facts.

    Args:
        query: Search query string. Be specific for better results.
        categories: Comma-separated categories. Options: general, images, news, videos, it, science, files, music, social media. Default: general.
        engines: Restrict to specific engines (comma-separated, e.g. "google,duckduckgo,brave"). Empty = all engines in the category.
        language: Language code for results (default: en).
        max_results: Maximum number of results (default: 10, max: 30).
        time_range: Time filter: day, week, month, year. Empty for no filter.

    Returns:
        JSON with search results (title, url, content snippet, engine), suggestions, and infoboxes.
    """
    max_results = min(max_results, 30)
    data = await searxng.search(
        query=query,
        categories=categories,
        engines=engines,
        language=language,
        time_range=time_range,
    )
    if data.get("results"):
        data["results"] = data["results"][:max_results]
    return _format(data)


@mcp.tool(annotations=READ_ONLY)
async def search_news(
    query: str,
    time_range: str = "week",
    language: str = "en",
    max_results: int = 10,
) -> str:
    """Search recent news articles. Defaults to last week.

    Args:
        query: News search query.
        time_range: Time filter: day, week, month, year (default: week).
        language: Language code (default: en).
        max_results: Maximum number of results (default: 10, max: 30).

    Returns:
        JSON with news results including title, url, content snippet, published date, and source engine.
    """
    max_results = min(max_results, 30)
    data = await searxng.search(
        query=query,
        categories="news",
        language=language,
        time_range=time_range,
    )
    if data.get("results"):
        data["results"] = data["results"][:max_results]
    return _format(data)


@mcp.tool(annotations=READ_ONLY)
async def search_tech(
    query: str,
    engines: str = "",
    max_results: int = 10,
) -> str:
    """Search technical/IT content: documentation, Stack Overflow, GitHub, wikis.

    Args:
        query: Technical search query (e.g. "fastmcp SSE transport python").
        engines: Restrict to specific engines (optional). Empty = all IT engines.
        max_results: Maximum results (default: 10, max: 30).

    Returns:
        JSON with search results focused on technical content.
    """
    max_results = min(max_results, 30)
    data = await searxng.search(query=query, categories="it", engines=engines)
    if data.get("results"):
        data["results"] = data["results"][:max_results]
    return _format(data)


@mcp.tool(annotations=READ_ONLY)
async def search_deep(
    query: str,
    categories: str = "general",
    engines: str = "",
    max_results: int = 50,
    pages: int = 3,
    time_range: str = "",
) -> str:
    """Deep search: fetch multiple pages, deduplicate by URL, rank by engine consensus.

    Results found by multiple engines are boosted. Use this when you need
    comprehensive, trustworthy coverage on a topic or person.

    Args:
        query: Search query string.
        categories: Category to search (default: general).
        engines: Restrict to specific engines (optional).
        max_results: Maximum deduplicated results (default: 50, max: 100).
        pages: Number of result pages to fetch (default: 3, max: 5).
        time_range: Time filter: day, week, month, year (optional).

    Returns:
        JSON with deduplicated results sorted by engine consensus. Each result includes engine_count.
    """
    max_results = min(max_results, 100)
    data = await searxng.search_deep(
        query=query,
        categories=categories,
        engines=engines,
        pages=pages,
        time_range=time_range,
    )
    if data.get("results"):
        data["results"] = data["results"][:max_results]
    return _format(data)


@mcp.tool(annotations=READ_ONLY)
async def search_images(
    query: str,
    max_results: int = 15,
    language: str = "en",
) -> str:
    """Search the web for images.

    Args:
        query: Image search query.
        max_results: Maximum number of image results (default: 15, max: 50).
        language: Language code (default: en).

    Returns:
        JSON with image results including title, source url, image url, thumbnail, and engine.
    """
    max_results = min(max_results, 50)
    data = await searxng.search(query=query, categories="images", language=language)
    if data.get("results"):
        data["results"] = data["results"][:max_results]
    return _format(data)


@mcp.tool(annotations=READ_ONLY)
async def search_videos(
    query: str,
    max_results: int = 10,
    language: str = "en",
    time_range: str = "",
) -> str:
    """Search the web for videos.

    Args:
        query: Video search query.
        max_results: Maximum number of video results (default: 10, max: 30).
        language: Language code (default: en).
        time_range: Time filter: day, week, month, year (optional).

    Returns:
        JSON with video results including title, url, content, thumbnail, length, and engine.
    """
    max_results = min(max_results, 30)
    data = await searxng.search(
        query=query,
        categories="videos",
        language=language,
        time_range=time_range,
    )
    if data.get("results"):
        data["results"] = data["results"][:max_results]
    return _format(data)


# ============================================================
# URL reader
# ============================================================


@mcp.tool(annotations=READ_ONLY)
async def read_url(url: str, max_chars: int = 0) -> str:
    """Fetch a URL and return its main content as clean markdown.

    Strips boilerplate (nav, ads, footers) using trafilatura. Use after a
    search call to read the top result and cite specific passages.

    Args:
        url: The URL to fetch.
        max_chars: If > 0, truncate the markdown to this many characters. Default 0 = no truncation.

    Returns:
        JSON with {url, title, markdown, length, fetched_status, extraction}.
        On failure (timeout, 4xx/5xx, no extractable content) returns {error, url}.
        URLs that target private/loopback/link-local/reserved addresses or a
        non-http(s) scheme are rejected before any fetch with
        {error, code: "BLOCKED_URL", url}. Redirect targets are held to the same
        check on every hop, so a public host cannot bounce the fetch inward.
    """
    try:
        data = await reader.read(url)
    except SsrfError as e:
        return _format(
            {"error": f"URL rejected by SSRF guard: {e}", "code": "BLOCKED_URL", "url": url}
        )
    except Exception as e:
        return _format({"error": f"{type(e).__name__}: {e}", "url": url})

    if max_chars > 0 and len(data.get("markdown", "")) > max_chars:
        data["markdown"] = data["markdown"][:max_chars]
        data["truncated"] = True
    return _format(data)


# ============================================================
# Engine listing
# ============================================================


@mcp.tool(annotations=READ_ONLY)
async def get_engines() -> str:
    """List enabled search engines and categories on this SearXNG instance.

    Useful for discovering which engines you can pass to the 'engines' parameter.

    Returns:
        JSON with instance info, engine list (name, categories, enabled status), and available categories.
    """
    data = await searxng.get_config()
    return _format(data)


# ============================================================
# Entry point
# ============================================================


def main() -> None:
    run_server(mcp, default_port=3702, default_transport="streamable-http")


if __name__ == "__main__":
    main()
