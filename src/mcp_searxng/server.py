"""MCP SearXNG - MCP server for self-hosted SearXNG metasearch.

Provides Claude Code tools for web search, news, deep multi-page search,
person/people-vetting fan-out, image and video search, and URL reading
via the Model Context Protocol (Streamable HTTP transport).

Designed as a grounded search backend for the /research and /vet skills.
"""

import json
import logging
import os
import sys

from dotenv import load_dotenv
from fastmcp import FastMCP

from mcp_searxng.clients.reader import UrlReader
from mcp_searxng.clients.searxng import SearxngClient
from mcp_searxng.clients.ssrf import SsrfError

load_dotenv()

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s  %(name)s  %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
)

SEARXNG_URL = os.getenv("SEARXNG_URL")

if not SEARXNG_URL:
    print("FATAL: Missing required environment variable:", file=sys.stderr)
    print("  SEARXNG_URL (SearXNG base URL, e.g. http://your-searxng-host:8888)", file=sys.stderr)
    print("\nCopy .env.example to .env and fill in your values.", file=sys.stderr)
    sys.exit(1)

CACHE_TTL = int(os.getenv("SEARXNG_CACHE_TTL", "300"))

searxng = SearxngClient(url=SEARXNG_URL, cache_ttl=CACHE_TTL)
reader = UrlReader()

mcp = FastMCP("SearXNG Search")


def _format(data: object) -> str:
    """Format response data as readable JSON string."""
    return json.dumps(data, indent=2, default=str)


# ============================================================
# Person / vetting (the headline tool)
# ============================================================


@mcp.tool()
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


@mcp.tool()
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


@mcp.tool()
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


@mcp.tool()
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


@mcp.tool()
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


@mcp.tool()
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


@mcp.tool()
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


@mcp.tool()
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
        {error, code: "BLOCKED_URL", url}.
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


@mcp.tool()
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
    host = os.getenv("FASTMCP_HOST", os.getenv("MCP_HOST", "0.0.0.0"))
    port = os.getenv("FASTMCP_PORT", os.getenv("MCP_PORT", "3702"))
    os.environ["FASTMCP_HOST"] = host
    os.environ["FASTMCP_PORT"] = str(port)
    print(f"Starting MCP SearXNG on {host}:{port} (Streamable HTTP transport)")
    mcp.run(transport="streamable-http")


if __name__ == "__main__":
    main()
