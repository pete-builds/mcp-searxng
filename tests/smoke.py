"""Live smoke test for SearxngClient + UrlReader.

Exercises every public method against a real SearXNG instance and a real
URL fetch. Catches the class of bug a unit test with mocks would miss
(e.g. wrong kwarg names that get swallowed by a bare except, broken
trafilatura extraction).

Usage:
    SEARXNG_URL=http://192.168.86.20:8888 python tests/smoke.py
"""

import asyncio
import os
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))

from mcp_searxng.clients.reader import UrlReader  # noqa: E402
from mcp_searxng.clients.searxng import SearxngClient  # noqa: E402


def ok(name: str) -> None:
    print(f"  PASS  {name}")


def fail(name: str, err: Exception) -> None:
    print(f"  FAIL  {name}: {type(err).__name__}: {err}")


async def main() -> int:
    url = os.getenv("SEARXNG_URL")
    if not url:
        print("SEARXNG_URL not set", file=sys.stderr)
        return 2

    client = SearxngClient(url=url)
    reader = UrlReader()
    failures = 0

    print(f"Smoke test against {url}\n")

    try:
        data = await client.search(query="python asyncio", categories="general")
        assert data["results"], "search returned no results"
        ok("search")
    except Exception as e:
        fail("search", e); failures += 1

    try:
        data = await client.search(query="anthropic claude", categories="news", time_range="month")
        assert data["results"], "news search returned no results"
        ok("search news category")
    except Exception as e:
        fail("search news category", e); failures += 1

    try:
        data = await client.search(query="fastmcp", categories="it")
        assert data["results"], "tech search returned no results"
        ok("search it category")
    except Exception as e:
        fail("search it category", e); failures += 1

    try:
        data = await client.search(query="grand canyon", categories="images")
        assert data["results"], "image search returned no results"
        ok("search images category")
    except Exception as e:
        fail("search images category", e); failures += 1

    try:
        data = await client.search(query="phish wilson", categories="videos")
        assert data["results"], "video search returned no results"
        ok("search videos category")
    except Exception as e:
        fail("search videos category", e); failures += 1

    try:
        data = await client.search_deep(query="model context protocol", pages=2)
        assert data["results"], "search_deep returned no results"
        assert all("engine_count" in r for r in data["results"]), "missing engine_count"
        ok("search_deep")
    except Exception as e:
        fail("search_deep", e); failures += 1

    try:
        data = await client.search_person(name="Tim Cook", location="Cupertino")
        assert data["total_results"] > 0, "search_person returned zero (regression: max_results bug)"
        assert any(v > 0 for v in data["by_category"].values()), "no category yielded results"
        ok("search_person")
    except Exception as e:
        fail("search_person", e); failures += 1

    try:
        data = await client.get_config()
        assert data["engines_count"] > 0, "get_config returned no engines"
        ok("get_config")
    except Exception as e:
        fail("get_config", e); failures += 1

    # Cache hit check: same query immediately should bypass network
    try:
        first = await client.search(query="cache test query", categories="general")
        cached = await client.search(query="cache test query", categories="general")
        assert first["results"] == cached["results"], "cache returned different payload"
        ok("ttl cache")
    except Exception as e:
        fail("ttl cache", e); failures += 1

    # URL reader against a stable text-heavy page
    try:
        data = await reader.read("https://en.wikipedia.org/wiki/Model_Context_Protocol")
        assert data["markdown"], "reader returned empty markdown"
        assert data["length"] > 500, f"markdown too short: {data['length']}"
        assert "trafilatura" == data["extraction"], "fell back to raw extraction"
        ok("read_url (wikipedia)")
    except Exception as e:
        fail("read_url (wikipedia)", e); failures += 1

    print(f"\n{'OK' if failures == 0 else 'FAILED'}: {failures} failure(s)")
    return 1 if failures else 0


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
