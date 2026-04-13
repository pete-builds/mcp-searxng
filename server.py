"""MCP SearXNG - MCP server for self-hosted SearXNG metasearch.

Provides Claude Code tools for web search, news search, and engine discovery
via the Model Context Protocol (SSE transport).

Designed as a grounded search backend for the /research skill.
"""

import json
import logging
import os
import sys

from dotenv import load_dotenv
from fastmcp import FastMCP

from clients.searxng import SearxngClient
from exceptions import SearxngError

load_dotenv()

# --- Query logging ---
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s  %(name)s  %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
)
logger = logging.getLogger(__name__)

# --- Config validation ---
SEARXNG_URL = os.getenv("SEARXNG_URL")

if not SEARXNG_URL:
    print("FATAL: Missing required environment variable:", file=sys.stderr)
    print("  SEARXNG_URL (SearXNG base URL, e.g. http://your-searxng-host:8888)", file=sys.stderr)
    print("\nCopy .env.example to .env and fill in your values.", file=sys.stderr)
    sys.exit(1)

# --- Initialize client ---
searxng = SearxngClient(url=SEARXNG_URL)

# --- MCP Server ---
mcp = FastMCP("SearXNG Search")


def _format(data: object) -> str:
    """Format response data as readable JSON string."""
    return json.dumps(data, indent=2, default=str)


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
    """Search the web using SearXNG metasearch engine. Aggregates results from multiple search engines.

    Best for: general web queries, finding documentation, researching topics, looking up facts.

    Args:
        query: The search query string. Be specific for better results.
        categories: Comma-separated categories to search. Options: general, images, news, videos, it, science, files, music, social media. Default: general.
        engines: Restrict to specific engines (comma-separated, e.g. "google,duckduckgo,brave"). Leave empty to use all engines in the category.
        language: Language code for results (default: en).
        max_results: Maximum number of results to return (default: 10, max: 30).
        time_range: Filter results by time: day, week, month, year. Leave empty for no time filter.

    Returns:
        JSON with search results (title, url, content snippet, engine), suggestions, and infoboxes.
    """
    max_results = min(max_results, 30)
    try:
        data = await searxng.search(
            query=query,
            categories=categories,
            engines=engines,
            language=language,
            time_range=time_range,
        )
    except SearxngError as exc:
        logger.error("search failed: %s", exc)
        return _format({"error": str(exc), "query": query})

    # Trim results to requested max
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
    """Search for recent news articles using SearXNG.

    Args:
        query: The news search query.
        time_range: Time filter: day, week, month, year (default: week).
        language: Language code for results (default: en).
        max_results: Maximum number of results (default: 10, max: 30).

    Returns:
        JSON with news results including title, url, content snippet, published date, and source engine.
    """
    max_results = min(max_results, 30)
    try:
        data = await searxng.search(
            query=query,
            categories="news",
            language=language,
            time_range=time_range,
        )
    except SearxngError as exc:
        logger.error("search_news failed: %s", exc)
        return _format({"error": str(exc), "query": query})

    if data.get("results"):
        data["results"] = data["results"][:max_results]

    return _format(data)


@mcp.tool()
async def search_tech(
    query: str,
    engines: str = "",
    max_results: int = 10,
) -> str:
    """Search for technical/IT content: documentation, Stack Overflow, GitHub, wikis.

    Args:
        query: Technical search query (e.g. "fastmcp SSE transport python").
        engines: Restrict to specific engines (optional). Leave empty for all IT engines.
        max_results: Maximum number of results (default: 10, max: 30).

    Returns:
        JSON with search results focused on technical content.
    """
    max_results = min(max_results, 30)
    try:
        data = await searxng.search(
            query=query,
            categories="it",
            engines=engines,
        )
    except SearxngError as exc:
        logger.error("search_tech failed: %s", exc)
        return _format({"error": str(exc), "query": query})

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
    """Deep search: fetch multiple pages of results and deduplicate by URL.

    Results found by multiple engines are boosted and ranked higher.
    Use this when you need comprehensive coverage on a topic or person.

    Args:
        query: The search query string.
        categories: Category to search (default: general).
        engines: Restrict to specific engines (optional).
        max_results: Maximum deduplicated results to return (default: 50, max: 100).
        pages: Number of result pages to fetch (default: 3, max: 5).
        time_range: Filter by time: day, week, month, year (optional).

    Returns:
        JSON with deduplicated results sorted by engine consensus. Each result includes engine_count (how many engines found it).
    """
    max_results = min(max_results, 100)
    try:
        data = await searxng.search_deep(
            query=query,
            categories=categories,
            engines=engines,
            pages=pages,
            time_range=time_range,
        )
    except SearxngError as exc:
        logger.error("search_deep failed: %s", exc)
        return _format({"error": str(exc), "query": query})

    if data.get("results"):
        data["results"] = data["results"][:max_results]

    return _format(data)


@mcp.tool()
async def search_person(
    name: str,
    location: str = "",
    context: str = "",
) -> str:
    """Search for a person across multiple angles in a single call.

    Automatically fans out 8 targeted searches: identity, LinkedIn, business filings,
    court/legal records, news, social media, property records, and Reddit. Results are
    deduplicated and categorized.

    Use this for people vetting, due diligence, or background research. Much more efficient
    than running 8+ separate search calls.

    Args:
        name: Full name of the person (e.g. "John Smith"). Will be quoted automatically.
        location: City/state/region to narrow results (e.g. "Ithaca NY"). Optional but recommended.
        context: Additional context like employer, business name, or role (e.g. "owns a construction company"). Optional.

    Returns:
        JSON with categorized results (identity, professional, business, legal, news, social, property, reddit),
        plus a deduplicated master list sorted by multi-engine consensus.
    """
    try:
        data = await searxng.search_person(
            name=name,
            location=location,
            context=context,
        )
    except SearxngError as exc:
        logger.error("search_person failed: %s", exc)
        return _format({"error": str(exc), "name": name})

    return _format(data)


@mcp.tool()
async def get_engines() -> str:
    """List all enabled search engines and categories on this SearXNG instance.

    Useful for discovering which engines are available to pass to the 'engines' parameter of search tools.

    Returns:
        JSON with instance info, engine list (name, categories, enabled status), and available categories.
    """
    try:
        data = await searxng.get_config()
    except SearxngError as exc:
        logger.error("get_engines failed: %s", exc)
        return _format({"error": str(exc)})

    return _format(data)


# ============================================================
# Entry point
# ============================================================

if __name__ == "__main__":
    host = os.getenv("FASTMCP_HOST", os.getenv("MCP_HOST", "0.0.0.0"))
    port = os.getenv("FASTMCP_PORT", os.getenv("MCP_PORT", "3702"))
    os.environ["FASTMCP_HOST"] = host
    os.environ["FASTMCP_PORT"] = str(port)
    print(f"Starting MCP SearXNG on {host}:{port} (SSE transport)")
    mcp.run(transport="sse")
