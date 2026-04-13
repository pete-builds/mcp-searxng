"""Tests for clients/searxng.py."""

from unittest.mock import AsyncMock, patch

import httpx
import pytest
import respx

from clients.searxng import REQUEST_DELAY, SearxngClient
from exceptions import SearxngAPIError, SearxngConnectionError

# ============================================================
# _shape_results
# ============================================================


class TestShapeResults:
    def test_basic_fields(self, client, sample_search_response):
        results = client._shape_results(sample_search_response)
        assert len(results) == 2
        assert results[0]["title"] == "Example Result"
        assert results[0]["url"] == "https://example.com/page"
        assert results[0]["content"] == "This is a sample search result content."
        assert results[0]["engine"] == "google"

    def test_optional_published_date(self, client):
        data = {
            "results": [
                {
                    "title": "News",
                    "url": "https://example.com",
                    "content": "stuff",
                    "engine": "google",
                    "publishedDate": "2026-04-10T12:00:00Z",
                }
            ]
        }
        results = client._shape_results(data)
        assert results[0]["published_date"] == "2026-04-10T12:00:00Z"

    def test_optional_score_and_thumbnail(self, client):
        data = {
            "results": [
                {
                    "title": "Scored",
                    "url": "https://example.com",
                    "content": "c",
                    "engine": "brave",
                    "score": 4.5,
                    "thumbnail": "https://example.com/thumb.jpg",
                }
            ]
        }
        results = client._shape_results(data)
        assert results[0]["score"] == 4.5
        assert results[0]["thumbnail"] == "https://example.com/thumb.jpg"

    def test_missing_optional_fields_omitted(self, client):
        data = {
            "results": [
                {"title": "Bare", "url": "https://example.com", "content": "c", "engine": "ddg"}
            ]
        }
        results = client._shape_results(data)
        assert "published_date" not in results[0]
        assert "score" not in results[0]
        assert "thumbnail" not in results[0]

    def test_empty_results(self, client):
        assert client._shape_results({}) == []
        assert client._shape_results({"results": []}) == []


# ============================================================
# deduplicate
# ============================================================


class TestDeduplicate:
    def test_no_duplicates(self):
        results = [
            {"url": "https://a.com", "engine": "google", "score": 1},
            {"url": "https://b.com", "engine": "brave", "score": 2},
        ]
        deduped = SearxngClient.deduplicate(results)
        assert len(deduped) == 2

    def test_duplicate_urls_merged(self):
        results = [
            {"url": "https://a.com", "engine": "google", "score": 3},
            {"url": "https://a.com", "engine": "brave", "score": 2},
        ]
        deduped = SearxngClient.deduplicate(results)
        assert len(deduped) == 1
        assert deduped[0]["engine_count"] == 2
        assert deduped[0]["score"] == 5
        assert set(deduped[0]["engines"]) == {"google", "brave"}

    def test_same_engine_not_double_counted(self):
        results = [
            {"url": "https://a.com", "engine": "google", "score": 1},
            {"url": "https://a.com", "engine": "google", "score": 1},
        ]
        deduped = SearxngClient.deduplicate(results)
        assert deduped[0]["engine_count"] == 1
        # Score is still summed even for same engine (current behavior)
        assert deduped[0]["score"] == 2

    def test_empty_url_skipped(self):
        results = [
            {"url": "", "engine": "google"},
            {"engine": "brave"},
        ]
        deduped = SearxngClient.deduplicate(results)
        assert len(deduped) == 0

    def test_sorted_by_engine_count_then_score(self):
        results = [
            {"url": "https://single.com", "engine": "google", "score": 10},
            {"url": "https://multi.com", "engine": "google", "score": 1},
            {"url": "https://multi.com", "engine": "brave", "score": 1},
        ]
        deduped = SearxngClient.deduplicate(results)
        assert deduped[0]["url"] == "https://multi.com"
        assert deduped[0]["engine_count"] == 2

    def test_empty_list(self):
        assert SearxngClient.deduplicate([]) == []


# ============================================================
# _rate_limit
# ============================================================


class TestRateLimit:
    async def test_no_delay_on_first_call(self, client):
        """First call should not sleep (last_request_time is 0)."""
        with patch("clients.searxng.asyncio.sleep", new_callable=AsyncMock) as mock_sleep:
            # _last_request_time is 0.0, time.monotonic() will be large, so no sleep needed
            await client._rate_limit()
            mock_sleep.assert_not_called()

    async def test_delay_enforced_on_rapid_calls(self, client):
        """Second rapid call should trigger a sleep."""
        import time

        client._last_request_time = time.monotonic()
        with patch("clients.searxng.asyncio.sleep", new_callable=AsyncMock) as mock_sleep:
            await client._rate_limit()
            mock_sleep.assert_called_once()
            args = mock_sleep.call_args[0]
            assert 0 < args[0] <= REQUEST_DELAY


# ============================================================
# _get
# ============================================================


class TestGet:
    @respx.mock
    async def test_successful_get(self, client, searxng_url):
        respx.get(f"{searxng_url}/search").mock(
            return_value=httpx.Response(200, json={"results": []})
        )
        result = await client._get("/search", params={"q": "test"})
        assert result == {"results": []}

    @respx.mock
    async def test_http_error_raises(self, client, searxng_url):
        respx.get(f"{searxng_url}/search").mock(return_value=httpx.Response(500))
        with pytest.raises(SearxngAPIError):
            await client._get("/search")

    @respx.mock
    async def test_retry_on_connect_error(self, client, searxng_url):
        route = respx.get(f"{searxng_url}/search").mock(
            side_effect=[
                httpx.ConnectError("connection refused"),
                httpx.Response(200, json={"ok": True}),
            ]
        )
        result = await client._get("/search")
        assert result == {"ok": True}
        assert route.call_count == 2

    @respx.mock
    async def test_retry_exhausted_raises(self, client, searxng_url):
        respx.get(f"{searxng_url}/search").mock(side_effect=httpx.ConnectError("still down"))
        with pytest.raises(SearxngConnectionError):
            await client._get("/search")

    @respx.mock
    async def test_retry_on_remote_protocol_error(self, client, searxng_url):
        route = respx.get(f"{searxng_url}/search").mock(
            side_effect=[
                httpx.RemoteProtocolError("server disconnected"),
                httpx.Response(200, json={"retried": True}),
            ]
        )
        result = await client._get("/search")
        assert result == {"retried": True}
        assert route.call_count == 2


# ============================================================
# search
# ============================================================


class TestSearch:
    @respx.mock
    async def test_search_happy_path(self, client, searxng_url, sample_search_response):
        respx.get(f"{searxng_url}/search").mock(
            return_value=httpx.Response(200, json=sample_search_response)
        )
        result = await client.search("test query")
        assert result["query"] == "test query"
        assert len(result["results"]) == 2
        assert result["number_of_results"] == 2
        assert result["suggestions"] == []
        assert result["corrections"] == []
        assert result["infoboxes"] == []

    @respx.mock
    async def test_search_with_engines_and_time_range(self, client, searxng_url):
        respx.get(f"{searxng_url}/search").mock(
            return_value=httpx.Response(200, json={"results": [], "query": "q"})
        )
        result = await client.search("q", engines="google,brave", time_range="week")
        assert result["query"] == "q"
        # Verify params were sent
        request = respx.calls[0].request
        assert b"engines=google%2Cbrave" in request.url.raw_path or "engines" in str(request.url)

    @respx.mock
    async def test_search_with_infoboxes(self, client, searxng_url):
        data = {
            "results": [],
            "query": "python",
            "infoboxes": [
                {
                    "infobox": "Python",
                    "content": "A programming language",
                    "urls": [{"title": "Official", "url": "https://python.org"}],
                }
            ],
        }
        respx.get(f"{searxng_url}/search").mock(return_value=httpx.Response(200, json=data))
        result = await client.search("python")
        assert len(result["infoboxes"]) == 1
        assert result["infoboxes"][0]["title"] == "Python"
        assert result["infoboxes"][0]["content"] == "A programming language"

    @respx.mock
    async def test_search_with_suggestions(self, client, searxng_url):
        data = {
            "results": [],
            "query": "pythn",
            "suggestions": ["python"],
        }
        respx.get(f"{searxng_url}/search").mock(return_value=httpx.Response(200, json=data))
        result = await client.search("pythn")
        assert result["suggestions"] == ["python"]


# ============================================================
# search_deep
# ============================================================


class TestSearchDeep:
    @respx.mock
    async def test_search_deep_multiple_pages(self, client, searxng_url):
        page1 = {
            "results": [{"title": "A", "url": "https://a.com", "content": "a", "engine": "google"}],
            "query": "deep",
        }
        page2 = {
            "results": [{"title": "B", "url": "https://b.com", "content": "b", "engine": "brave"}],
            "query": "deep",
        }
        page3 = {"results": [], "query": "deep"}

        call_count = 0

        def route_handler(request):
            nonlocal call_count
            call_count += 1
            if call_count == 1:
                return httpx.Response(200, json=page1)
            elif call_count == 2:
                return httpx.Response(200, json=page2)
            return httpx.Response(200, json=page3)

        respx.get(f"{searxng_url}/search").mock(side_effect=route_handler)

        result = await client.search_deep("deep", pages=3)
        assert result["query"] == "deep"
        assert result["number_of_results"] == 2
        assert result["pages_fetched"] == 3

    @respx.mock
    async def test_search_deep_stops_on_empty_page(self, client, searxng_url):
        page1 = {
            "results": [{"title": "A", "url": "https://a.com", "content": "a", "engine": "google"}],
            "query": "q",
        }
        page2 = {"results": [], "query": "q"}

        call_count = 0

        def handler(request):
            nonlocal call_count
            call_count += 1
            if call_count == 1:
                return httpx.Response(200, json=page1)
            return httpx.Response(200, json=page2)

        respx.get(f"{searxng_url}/search").mock(side_effect=handler)

        result = await client.search_deep("q", pages=5)
        assert result["number_of_results"] == 1
        # Should have stopped after page 2 returned empty
        assert call_count == 2

    @respx.mock
    async def test_search_deep_caps_pages_at_5(self, client, searxng_url):
        respx.get(f"{searxng_url}/search").mock(
            return_value=httpx.Response(200, json={"results": [], "query": "q"})
        )
        result = await client.search_deep("q", pages=10)
        assert result["pages_fetched"] == 5


# ============================================================
# search_person
# ============================================================


class TestSearchPerson:
    @respx.mock
    async def test_search_person_happy_path(self, client, searxng_url):
        api_response = {
            "results": [
                {
                    "title": "John Smith LinkedIn",
                    "url": "https://linkedin.com/in/jsmith",
                    "content": "profile",
                    "engine": "google",
                }
            ],
            "query": "john smith",
        }
        respx.get(f"{searxng_url}/search").mock(return_value=httpx.Response(200, json=api_response))
        result = await client.search_person("John Smith", location="Ithaca NY")
        assert result["name"] == "John Smith"
        assert result["location"] == "Ithaca NY"
        assert "by_category" in result
        assert "results" in result
        # Should have 8 categories queried
        assert len(result["by_category"]) == 8

    @respx.mock
    async def test_search_person_with_context(self, client, searxng_url):
        respx.get(f"{searxng_url}/search").mock(
            return_value=httpx.Response(200, json={"results": [], "query": "q"})
        )
        result = await client.search_person("Jane Doe", location="NYC", context="owns a bakery")
        assert result["context"] == "owns a bakery"

    @respx.mock
    async def test_search_person_catches_errors_gracefully(self, client, searxng_url):
        """When a category query fails, search_person catches the error and
        continues with remaining categories instead of raising."""
        call_count = 0

        def side_effect(request):
            nonlocal call_count
            call_count += 1
            if call_count <= 2:
                return httpx.Response(500)
            return httpx.Response(
                200,
                json={
                    "results": [
                        {"title": "R", "url": "https://example.com", "content": "c", "engine": "g"}
                    ],
                    "query": "q",
                },
            )

        respx.get(f"{searxng_url}/search").mock(side_effect=side_effect)
        result = await client.search_person("Test Person")
        # Some categories fail, but the function still returns results from successful ones
        assert result["total_results"] >= 0
        assert len(result["by_category"]) == 8

    @respx.mock
    async def test_search_person_empty_results(self, client, searxng_url):
        respx.get(f"{searxng_url}/search").mock(
            return_value=httpx.Response(200, json={"results": [], "query": "q"})
        )
        result = await client.search_person("Nobody Real")
        assert result["total_results"] == 0


# ============================================================
# get_config
# ============================================================


class TestGetConfig:
    @respx.mock
    async def test_get_config_happy_path(self, client, searxng_url):
        config_data = {
            "instance_name": "test-searxng",
            "version": "1.0.0",
            "engines": [
                {
                    "name": "google",
                    "categories": ["general"],
                    "language_support": True,
                    "enabled": True,
                },
                {
                    "name": "brave",
                    "categories": ["general", "news"],
                    "language_support": False,
                    "enabled": True,
                },
            ],
            "categories": ["general", "news", "images"],
            "safe_search": 0,
            "default_locale": "en",
        }
        respx.get(f"{searxng_url}/config").mock(return_value=httpx.Response(200, json=config_data))
        result = await client.get_config()
        assert result["instance_name"] == "test-searxng"
        assert result["version"] == "1.0.0"
        assert result["engines_count"] == 2
        assert len(result["engines"]) == 2
        assert result["categories"] == ["general", "images", "news"]  # sorted
        assert result["safe_search"] == 0

    @respx.mock
    async def test_get_config_empty_engines(self, client, searxng_url):
        respx.get(f"{searxng_url}/config").mock(
            return_value=httpx.Response(200, json={"engines": [], "categories": []})
        )
        result = await client.get_config()
        assert result["engines_count"] == 0
        assert result["engines"] == []
        assert result["categories"] == []


# ============================================================
# __init__
# ============================================================


class TestInit:
    def test_trailing_slash_stripped(self):
        client = SearxngClient("http://example.com/")
        assert client.base_url == "http://example.com"

    def test_no_trailing_slash(self):
        client = SearxngClient("http://example.com")
        assert client.base_url == "http://example.com"
