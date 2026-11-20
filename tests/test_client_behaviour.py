"""SearXNG client: rate limiting under concurrency, errors, dedup, ranking, filters.

Each class names what 0.5.0 got wrong. Hermetic: respx stands in for SearXNG.
"""

import asyncio
import logging
import sys
import time
from pathlib import Path

import httpx
import pytest
import respx

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))

from mcp_searxng.clients import searxng as mod  # noqa: E402

BASE = "http://searxng.test:8888"


def page(results, unresponsive=None):
    return {"query": "q", "number_of_results": len(results), "results": results,
            "unresponsive_engines": unresponsive or []}


def hit(url, engines=("bing",), score=1.0, title="t", content="c"):
    return {"url": url, "title": title, "content": content, "engine": engines[0],
            "engines": list(engines), "score": score}


@pytest.fixture
def client(monkeypatch):
    monkeypatch.setattr(mod, "REQUEST_DELAY", 0.0)
    return mod.SearxngClient(url=BASE, cache_ttl=0)


class TestRateLimitHoldsUnderConcurrency:
    """0.5.0: five concurrent requests went out with 0.0 s between them."""

    @pytest.mark.asyncio
    @respx.mock
    async def test_concurrent_requests_are_spaced(self, monkeypatch) -> None:
        monkeypatch.setattr(mod, "REQUEST_DELAY", 0.05)
        sent: list[float] = []

        def record(request):
            sent.append(time.monotonic())
            return httpx.Response(200, json=page([]))

        respx.get(f"{BASE}/search").mock(side_effect=record)
        c = mod.SearxngClient(url=BASE, cache_ttl=0)
        await asyncio.gather(*(c.search(f"q{i}") for i in range(4)))
        gaps = [b - a for a, b in zip(sent, sent[1:])]
        assert len(sent) == 4
        assert min(gaps) >= 0.045, f"requests not spaced: {gaps}"

    @pytest.mark.asyncio
    @respx.mock
    async def test_min_interval_zero_disables_spacing(self, monkeypatch) -> None:
        monkeypatch.setattr(mod, "REQUEST_DELAY", 5.0)  # would stall the test if used
        respx.get(f"{BASE}/search").mock(return_value=httpx.Response(200, json=page([])))
        c = mod.SearxngClient(url=BASE, cache_ttl=0, min_interval=0)
        started = time.monotonic()
        await asyncio.gather(*(c.search(f"z{i}") for i in range(3)))
        assert time.monotonic() - started < 1.0

    @pytest.mark.asyncio
    @respx.mock
    async def test_identical_concurrent_queries_share_one_request(self, client) -> None:
        route = respx.get(f"{BASE}/search").mock(
            return_value=httpx.Response(200, json=page([hit("https://a.example/")]))
        )
        out = await asyncio.gather(*(client.search("same query") for _ in range(5)))
        assert route.call_count == 1
        assert all(o["results"][0]["url"] == "https://a.example/" for o in out)


class TestErrorsNameTheFix:
    """0.5.0 raised raw httpx errors carrying the internal SearXNG URL."""

    @pytest.mark.asyncio
    @pytest.mark.parametrize("status,code,needle", [
        (403, "SEARXNG_FORBIDDEN", "search.formats"),
        (429, "SEARXNG_RATE_LIMITED", "limiter"),
        (502, "SEARXNG_HTTP_ERROR", "502"),
    ])
    @respx.mock
    async def test_status_errors(self, client, status, code, needle) -> None:
        respx.get(f"{BASE}/search").mock(return_value=httpx.Response(status))
        with pytest.raises(mod.SearxngError) as exc:
            await client.search("q")
        assert exc.value.code == code
        assert needle in str(exc.value)
        assert "searxng.test" not in str(exc.value)

    @pytest.mark.asyncio
    @respx.mock
    async def test_html_instead_of_json(self, client) -> None:
        respx.get(f"{BASE}/search").mock(
            return_value=httpx.Response(200, headers={"content-type": "text/html"},
                                        content=b"<html>results</html>")
        )
        with pytest.raises(mod.SearxngError) as exc:
            await client.search("q")
        assert exc.value.code == "SEARXNG_NOT_JSON"

    @pytest.mark.asyncio
    @respx.mock
    async def test_unreachable(self, client) -> None:
        respx.get(f"{BASE}/search").mock(side_effect=httpx.ConnectError("refused"))
        with pytest.raises(mod.SearxngError) as exc:
            await client.search("q")
        assert exc.value.code == "SEARXNG_UNREACHABLE"


class TestDeepSweep:
    @pytest.mark.asyncio
    async def test_url_variants_of_one_page_merge(self, client, monkeypatch) -> None:
        """0.5.0 deduplicated on the exact string only."""
        pages = {
            1: page([hit("https://www.example.com/a/", ("bing",))]),
            2: page([hit("http://example.com/a?utm_source=x#top", ("duckduckgo",))]),
        }

        async def fake_get(path, params=None):
            return pages.get(params["pageno"], page([]))

        monkeypatch.setattr(client, "_get", fake_get)
        out = await client.search_deep("q", pages=2)
        assert len(out["results"]) == 1
        assert out["results"][0]["engines"] == ["bing", "duckduckgo"]
        assert out["results"][0]["url"] == "https://www.example.com/a/"  # an engine's own URL

    def test_distinct_pages_stay_distinct(self) -> None:
        a = mod.canonical_url("https://example.com/item?id=1")
        b = mod.canonical_url("https://example.com/item?id=2")
        assert a != b

    @pytest.mark.asyncio
    async def test_page_one_top_result_is_not_buried_by_consensus(self, client, monkeypatch) -> None:
        """0.5.0 sorted by engine_count first, so a 2-engine page-3 hit beat
        SearXNG's own #1. The benchmark showed that lowered nDCG@10."""
        pages = {
            1: page([hit("https://top.example/", ("bing",), score=9.0)]
                    + [hit(f"https://p1.example/{i}", ("bing",), score=1.0) for i in range(12)]),
            2: page([hit("https://corroborated.example/", ("bing", "yahoo"), score=0.5)]),
        }

        async def fake_get(path, params=None):
            return pages.get(params["pageno"], page([]))

        monkeypatch.setattr(client, "_get", fake_get)
        out = await client.search_deep("q", pages=2)
        urls = [r["url"] for r in out["results"]]
        assert urls[0] == "https://top.example/"
        assert urls.index("https://corroborated.example/") >= mod.DEEP_HEAD

    def test_tail_is_ordered_by_best_page_score(self) -> None:
        merged = [{"url": f"https://h.example/{i}"} for i in range(12)]
        raw = [{"url": f"https://h.example/{i}", "score": 1.0} for i in range(12)]
        raw[11]["score"] = 5.0  # strong hit at the very end of the first-seen order
        ranked = mod.SearxngClient.rank_deep(merged, raw, head=10)
        assert [r["url"] for r in ranked[:10]] == [m["url"] for m in merged[:10]]
        assert ranked[10]["url"] == "https://h.example/11"

    @pytest.mark.asyncio
    @pytest.mark.parametrize("pages,expected_calls", [(0, 1), (-3, 1), (99, 5)])
    async def test_page_bounds(self, client, monkeypatch, pages, expected_calls) -> None:
        calls = []

        async def fake_get(path, params=None):
            calls.append(params["pageno"])
            return page([hit(f"https://e.example/{params['pageno']}")])

        monkeypatch.setattr(client, "_get", fake_get)
        out = await client.search_deep("q", pages=pages)
        assert len(calls) == expected_calls
        assert out["pages_fetched"] == expected_calls

    @pytest.mark.asyncio
    async def test_pages_fetched_reports_what_was_fetched(self, client, monkeypatch) -> None:
        """0.5.0 echoed the requested count even when page 2 came back empty."""
        async def fake_get(path, params=None):
            return page([hit("https://e.example/1")]) if params["pageno"] == 1 else page([])

        monkeypatch.setattr(client, "_get", fake_get)
        out = await client.search_deep("q", pages=5)
        assert out["pages_fetched"] == 2
        assert out["pages_requested"] == 5


class TestDomainFilters:
    @pytest.mark.asyncio
    async def test_include_adds_site_and_filters(self, client, monkeypatch) -> None:
        seen_q = []

        async def fake_get(path, params=None):
            seen_q.append(params["q"])
            return page([hit("https://docs.python.org/3/x"), hit("https://blog.example/y"),
                         hit("https://python.org/z")])

        monkeypatch.setattr(client, "_get", fake_get)
        out = await client.search("asyncio", include_domains="https://www.python.org/")
        assert seen_q == ["asyncio site:python.org"]
        assert [r["url"] for r in out["results"]] == ["https://docs.python.org/3/x",
                                                       "https://python.org/z"]
        assert out["domain_filter"]["filtered_out"] == 1

    @pytest.mark.asyncio
    async def test_exclude_drops_subdomains_too(self, client, monkeypatch) -> None:
        async def fake_get(path, params=None):
            return page([hit("https://in.pinterest.com/p"), hit("https://keep.example/")])

        monkeypatch.setattr(client, "_get", fake_get)
        out = await client.search("q", exclude_domains="pinterest.com")
        assert [r["url"] for r in out["results"]] == ["https://keep.example/"]

    def test_lookalike_domain_does_not_match(self) -> None:
        assert not mod.host_matches("https://notpython.org/", ["python.org"])

    @pytest.mark.asyncio
    async def test_deep_sweep_continues_past_a_page_the_filter_emptied(self, client, monkeypatch) -> None:
        pages = {1: page([hit("https://other.example/1")]),
                 2: page([hit("https://docs.example/hit")])}

        async def fake_get(path, params=None):
            return pages.get(params["pageno"], page([]))

        monkeypatch.setattr(client, "_get", fake_get)
        out = await client.search_deep("q", pages=3, include_domains="docs.example")
        assert [r["url"] for r in out["results"]] == ["https://docs.example/hit"]


class TestSearchPerson:
    @pytest.mark.asyncio
    async def test_results_say_what_they_match_and_carry_a_caveat(self, client, monkeypatch) -> None:
        queries = []

        async def fake_get(path, params=None):
            queries.append(params["q"])
            return page([
                hit("https://a.example/", title="Jane Q Doe, Ithaca NY", content="profile"),
                hit("https://b.example/", title="Doe family history", content="unrelated"),
            ])

        monkeypatch.setattr(client, "_get", fake_get)
        out = await client.search_person("Jane Doe", location="Ithaca, NY")
        assert "not verified" in out["caveat"]
        first, second = out["results"][0], out["results"][1]
        assert first["match"] == {"name_in_text": True, "location_in_text": True}
        assert second["match"]["name_in_text"] is False
        assert out["name_matches"] == 1
        social = [q for q in queries if "site:facebook.com" in q][0]
        assert "site:x.com" in social and "Ithaca" in social

    @pytest.mark.asyncio
    async def test_every_angle_failing_is_an_error_not_zero_results(self, client, monkeypatch) -> None:
        async def broken(path, params=None):
            raise mod.SearxngError("SEARXNG_FORBIDDEN", "json disabled")

        monkeypatch.setattr(client, "_get", broken)
        with pytest.raises(mod.SearxngError):
            await client.search_person("Jane Doe")

    @pytest.mark.asyncio
    async def test_names_and_queries_are_not_logged_at_info(self, client, monkeypatch, caplog) -> None:
        async def fake_get(path, params=None):
            return page([hit("https://a.example/")])

        monkeypatch.setattr(client, "_get", fake_get)
        with caplog.at_level(logging.INFO, logger="searxng.queries"):
            await client.search_person("Zebulon Quixote")
            await client.search_deep("private research topic", pages=1)
        logged = " ".join(r.getMessage() for r in caplog.records if r.levelno >= logging.INFO)
        assert "Zebulon" not in logged
        assert "private research topic" not in logged
