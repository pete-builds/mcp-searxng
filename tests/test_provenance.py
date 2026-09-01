"""Cross-engine provenance on the search envelope.

Why this exists: on 2026-08-27 the SearXNG instance degraded to a single
working general engine (brave, startpage and duckduckgo were all CAPTCHA
blocked). Bing has no index for niche terms and returns unrelated filler
rather than an empty set, and SearXNG reported unresponsive_engines=[], so
a junk result set was indistinguishable from a healthy one at the tool
boundary. It reached a research report as if it were real sources.

These tests pin the signal that makes the two distinguishable.
"""

import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))

from mcp_searxng.clients.searxng import SearxngClient  # noqa: E402


def _client() -> SearxngClient:
    return SearxngClient(url="http://searxng.invalid")


def _raw(results, unresponsive=None):
    """Minimal SearXNG /search JSON payload."""
    return {
        "query": "q",
        "number_of_results": len(results),
        "results": results,
        "unresponsive_engines": unresponsive or [],
    }


SINGLE_ENGINE = _raw(
    [
        {"title": "t%d" % i, "url": "https://e.invalid/%d" % i,
         "content": "c", "engine": "bing", "engines": ["bing"], "score": 1.0}
        for i in range(10)
    ],
    unresponsive=[["brave", "CAPTCHA"], ["duckduckgo", "CAPTCHA"]],
)

MULTI_ENGINE = _raw(
    [
        {"title": "a", "url": "https://e.invalid/a", "content": "c",
         "engine": "bing", "engines": ["bing", "seznam"], "score": 3.0},
        {"title": "b", "url": "https://e.invalid/b", "content": "c",
         "engine": "seznam", "engines": ["seznam"], "score": 1.0},
        {"title": "c", "url": "https://e.invalid/c", "content": "c",
         "engine": "mwmbl", "engines": ["mwmbl"], "score": 1.0},
    ]
)


class TestPerResultEngines:
    def test_upstream_engines_list_is_preserved(self):
        """SearXNG merges cross-engine hits and reports `engines`. Do not drop it."""
        shaped = _client()._shape_results(MULTI_ENGINE)
        assert shaped[0]["engines"] == ["bing", "seznam"]
        assert shaped[0]["engine_count"] == 2

    def test_single_engine_result_counts_one(self):
        shaped = _client()._shape_results(MULTI_ENGINE)
        assert shaped[1]["engines"] == ["seznam"]
        assert shaped[1]["engine_count"] == 1


class TestProvenanceBlock:
    def test_degraded_when_one_engine_contributes(self):
        p = _client()._provenance(SINGLE_ENGINE, _client()._shape_results(SINGLE_ENGINE))
        assert p["degraded"] is True
        assert p["engine_count"] == 1
        assert p["engines_contributing"] == ["bing"]
        assert p["corroborated_results"] == 0
        assert p["single_engine_results"] == 10

    def test_degraded_warning_is_human_readable_and_names_the_engine(self):
        """A boolean alone gets skimmed past. The warning has to say it in words."""
        p = _client()._provenance(SINGLE_ENGINE, _client()._shape_results(SINGLE_ENGINE))
        assert p["warning"], "degraded response must carry a warning string"
        assert "DEGRADED" in p["warning"]
        assert "bing" in p["warning"]

    def test_unresponsive_engines_are_surfaced_not_swallowed(self):
        p = _client()._provenance(SINGLE_ENGINE, _client()._shape_results(SINGLE_ENGINE))
        names = [e[0] for e in p["unresponsive_engines"]]
        assert "brave" in names and "duckduckgo" in names

    def test_healthy_multi_engine_does_not_false_positive(self):
        """The control. A gate that always fires is as useless as one that never does."""
        p = _client()._provenance(MULTI_ENGINE, _client()._shape_results(MULTI_ENGINE))
        assert p["degraded"] is False
        assert p["warning"] is None
        assert p["engine_count"] == 3
        assert p["engines_contributing"] == ["bing", "mwmbl", "seznam"]
        assert p["corroborated_results"] == 1

    def test_empty_result_set_is_degraded_not_crashing(self):
        p = _client()._provenance(_raw([]), [])
        assert p["degraded"] is True
        assert p["engine_count"] == 0


class TestEnvelope:
    @pytest.mark.asyncio
    async def test_search_envelope_carries_provenance(self, monkeypatch):
        c = _client()

        async def fake_get(path, params=None):
            return SINGLE_ENGINE

        monkeypatch.setattr(c, "_get", fake_get)
        env = await c.search("q")
        assert "provenance" in env
        assert env["provenance"]["degraded"] is True
        # results must NOT be filtered out; visibility, not suppression
        assert len(env["results"]) == 10
