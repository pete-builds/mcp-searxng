"""The tool layer as an MCP client sees it: errors, bounds, output shape.

Calls go through fastmcp.Client over the in-memory transport, so what is
asserted is the text a client receives, not the return value of a function.
"""

import json
import os
import sys
from pathlib import Path

import httpx
import pytest

os.environ.setdefault("SEARXNG_URL", "http://searxng.invalid:8888")
os.environ.setdefault("MCP_AUTH_TOKEN", "test-token-not-a-secret")
sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))

import fastmcp  # noqa: E402

from mcp_searxng import server  # noqa: E402
from mcp_searxng.clients import searxng as searxng_mod  # noqa: E402


def _page(n: int) -> dict:
    return {
        "results": [
            {"url": f"https://e.example/{i}", "title": f"t{i}", "content": "café",
             "engine": "bing", "engines": ["bing", "yahoo"], "score": 1.0}
            for i in range(n)
        ],
        "unresponsive_engines": [],
    }


@pytest.fixture
def fake_searxng(monkeypatch):
    calls = []

    async def fake_get(path, params=None):
        calls.append(params)
        return _page(12)

    monkeypatch.setattr(server.searxng, "_get", fake_get)
    return calls


async def _call(name: str, args: dict):
    async with fastmcp.Client(server.mcp) as client:
        return await client.call_tool(name, args, raise_on_error=False)


class TestBounds:
    @pytest.mark.asyncio
    async def test_negative_max_results_is_clamped_not_sliced(self, fake_searxng) -> None:
        """0.5.0: results[:-1] silently dropped the last result."""
        res = await _call("search", {"query": "bounds", "max_results": -1})
        assert len(json.loads(res.content[0].text)["results"]) == 1

    @pytest.mark.asyncio
    async def test_max_results_upper_bound_still_applies(self, fake_searxng) -> None:
        res = await _call("search_images", {"query": "bounds", "max_results": 500})
        assert len(json.loads(res.content[0].text)["results"]) == 12  # all there were, cap 50


class TestOutputShape:
    @pytest.mark.asyncio
    async def test_output_is_compact_json_with_unescaped_text(self, fake_searxng) -> None:
        res = await _call("search", {"query": "shape"})
        text = res.content[0].text
        assert "\n" not in text, "indented JSON costs tokens the model never reads"
        assert "café" in text, "non-ASCII escaped as \\uXXXX"
        assert json.loads(text)["provenance"]["engine_count"] == 2


class TestSearxngFailuresAreActionable:
    @pytest.mark.asyncio
    async def test_json_disabled_instance(self, monkeypatch) -> None:
        async def forbidden(path, params=None):
            raise searxng_mod._status_error(403)

        monkeypatch.setattr(server.searxng, "_get", forbidden)
        res = await _call("search_deep", {"query": "x"})
        assert res.is_error
        msg = res.content[0].text
        assert "search.formats" in msg and "SEARXNG_FORBIDDEN" in msg
        assert "searxng.invalid" not in msg


class TestReadUrlErrors:
    @pytest.mark.asyncio
    async def test_http_error_reports_status_and_code(self, monkeypatch) -> None:
        async def fail(url):
            req = httpx.Request("GET", url)
            raise httpx.HTTPStatusError("x", request=req, response=httpx.Response(451, request=req))

        monkeypatch.setattr(server.reader, "read", fail)
        res = await _call("read_url", {"url": "https://e.example/"})
        data = json.loads(res.content[0].text)
        assert not res.is_error, "research relies on read_url failing inside a normal result"
        assert data["code"] == "HTTP_ERROR" and data["status"] == 451 and "error" in data

    @pytest.mark.asyncio
    async def test_blocked_url_keeps_its_code(self) -> None:
        res = await _call("read_url", {"url": "http://127.0.0.1:8080/"})
        data = json.loads(res.content[0].text)
        assert data["code"] == "BLOCKED_URL"

    @pytest.mark.asyncio
    async def test_truncation_reports_the_full_length(self, monkeypatch) -> None:
        async def ok(url):
            return {"url": url, "title": "t", "markdown": "x" * 1000, "length": 1000,
                    "fetched_status": 200, "extraction": "trafilatura"}

        monkeypatch.setattr(server.reader, "read", ok)
        res = await _call("read_url", {"url": "https://e.example/", "max_chars": 100})
        data = json.loads(res.content[0].text)
        assert data["truncated"] is True and data["total_length"] == 1000
        assert len(data["markdown"]) == 100


class TestDescriptions:
    @pytest.mark.asyncio
    async def test_no_tool_claims_engine_agreement_means_trust(self) -> None:
        async with fastmcp.Client(server.mcp) as client:
            tools = await client.list_tools()
        for t in tools:
            assert "trustworthy" not in (t.description or "").lower(), t.name

    @pytest.mark.asyncio
    async def test_search_person_says_matches_are_not_identity(self) -> None:
        async with fastmcp.Client(server.mcp) as client:
            tools = {t.name: t for t in await client.list_tools()}
        assert "not verified identity" in tools["search_person"].description
