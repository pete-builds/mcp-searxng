"""Tests for server.py MCP tool functions."""

import json
from unittest.mock import AsyncMock, patch

import pytest


# We need to patch the environment before importing server.py,
# which runs module-level validation.
@pytest.fixture(autouse=True)
def _patch_env(monkeypatch):
    monkeypatch.setenv("SEARXNG_URL", "http://searxng.test:8080")


# Import server components after env is set. We need to reload
# the module each time to pick up the patched env.
@pytest.fixture
def server_module():
    """Import server module with SEARXNG_URL set."""
    import importlib
    import server

    importlib.reload(server)
    return server


@pytest.fixture
def mock_searxng_client():
    """Create a mock SearxngClient that replaces the module-level instance."""
    return AsyncMock()


class TestFormat:
    def test_format_dict(self, server_module):
        result = server_module._format({"key": "value"})
        assert json.loads(result) == {"key": "value"}

    def test_format_list(self, server_module):
        result = server_module._format([1, 2, 3])
        assert json.loads(result) == [1, 2, 3]

    def test_format_with_non_serializable(self, server_module):
        """_format uses default=str for non-serializable types."""
        from datetime import datetime

        result = server_module._format({"ts": datetime(2026, 1, 1)})
        parsed = json.loads(result)
        assert "2026" in parsed["ts"]


class TestSearchTool:
    async def test_search_returns_json(self, server_module, mock_searxng_client):
        mock_searxng_client.search.return_value = {
            "query": "test",
            "results": [{"title": "R1", "url": "https://example.com"}],
            "number_of_results": 1,
        }
        with patch.object(server_module, "searxng", mock_searxng_client):
            result = await server_module.search("test")
        parsed = json.loads(result)
        assert parsed["query"] == "test"
        assert len(parsed["results"]) == 1

    async def test_search_trims_to_max_results(self, server_module, mock_searxng_client):
        many_results = [{"title": f"R{i}", "url": f"https://example.com/{i}"} for i in range(20)]
        mock_searxng_client.search.return_value = {
            "query": "test",
            "results": many_results,
            "number_of_results": 20,
        }
        with patch.object(server_module, "searxng", mock_searxng_client):
            result = await server_module.search("test", max_results=5)
        parsed = json.loads(result)
        assert len(parsed["results"]) == 5

    async def test_search_max_results_capped_at_30(self, server_module, mock_searxng_client):
        many_results = [{"title": f"R{i}", "url": f"https://example.com/{i}"} for i in range(50)]
        mock_searxng_client.search.return_value = {
            "query": "test",
            "results": many_results,
        }
        with patch.object(server_module, "searxng", mock_searxng_client):
            result = await server_module.search("test", max_results=50)
        parsed = json.loads(result)
        assert len(parsed["results"]) == 30

    async def test_search_empty_results(self, server_module, mock_searxng_client):
        mock_searxng_client.search.return_value = {"query": "nothing", "results": []}
        with patch.object(server_module, "searxng", mock_searxng_client):
            result = await server_module.search("nothing")
        parsed = json.loads(result)
        assert parsed["results"] == []


class TestSearchNewsTool:
    async def test_search_news_defaults(self, server_module, mock_searxng_client):
        mock_searxng_client.search.return_value = {
            "query": "news",
            "results": [{"title": "News Item"}],
        }
        with patch.object(server_module, "searxng", mock_searxng_client):
            result = await server_module.search_news("news")
        mock_searxng_client.search.assert_called_once_with(
            query="news",
            categories="news",
            language="en",
            time_range="week",
        )
        parsed = json.loads(result)
        assert len(parsed["results"]) == 1

    async def test_search_news_trims_results(self, server_module, mock_searxng_client):
        many = [{"title": f"N{i}"} for i in range(15)]
        mock_searxng_client.search.return_value = {"query": "q", "results": many}
        with patch.object(server_module, "searxng", mock_searxng_client):
            result = await server_module.search_news("q", max_results=5)
        parsed = json.loads(result)
        assert len(parsed["results"]) == 5


class TestSearchTechTool:
    async def test_search_tech_uses_it_category(self, server_module, mock_searxng_client):
        mock_searxng_client.search.return_value = {"query": "q", "results": []}
        with patch.object(server_module, "searxng", mock_searxng_client):
            await server_module.search_tech("python asyncio")
        mock_searxng_client.search.assert_called_once_with(
            query="python asyncio",
            categories="it",
            engines="",
        )


class TestSearchDeepTool:
    async def test_search_deep_passes_params(self, server_module, mock_searxng_client):
        mock_searxng_client.search_deep.return_value = {
            "query": "q",
            "results": [{"title": "R1"}],
            "number_of_results": 1,
            "pages_fetched": 3,
        }
        with patch.object(server_module, "searxng", mock_searxng_client):
            result = await server_module.search_deep("q", pages=3)
        parsed = json.loads(result)
        assert parsed["pages_fetched"] == 3

    async def test_search_deep_max_results_capped_at_100(self, server_module, mock_searxng_client):
        many = [{"title": f"R{i}"} for i in range(120)]
        mock_searxng_client.search_deep.return_value = {
            "query": "q",
            "results": many,
        }
        with patch.object(server_module, "searxng", mock_searxng_client):
            result = await server_module.search_deep("q", max_results=200)
        parsed = json.loads(result)
        assert len(parsed["results"]) == 100


class TestSearchPersonTool:
    async def test_search_person_passes_args(self, server_module, mock_searxng_client):
        mock_searxng_client.search_person.return_value = {
            "name": "John",
            "results": [],
            "total_results": 0,
        }
        with patch.object(server_module, "searxng", mock_searxng_client):
            result = await server_module.search_person("John", location="NYC", context="dev")
        mock_searxng_client.search_person.assert_called_once_with(
            name="John", location="NYC", context="dev"
        )
        parsed = json.loads(result)
        assert parsed["name"] == "John"


class TestGetEnginesTool:
    async def test_get_engines(self, server_module, mock_searxng_client):
        mock_searxng_client.get_config.return_value = {
            "instance_name": "test",
            "engines": [{"name": "google"}],
            "engines_count": 1,
        }
        with patch.object(server_module, "searxng", mock_searxng_client):
            result = await server_module.get_engines()
        parsed = json.loads(result)
        assert parsed["instance_name"] == "test"


class TestModuleLevelConfig:
    def test_missing_searxng_url_exits(self, monkeypatch):
        monkeypatch.delenv("SEARXNG_URL", raising=False)
        with pytest.raises(SystemExit) as exc_info:
            import importlib
            import server

            importlib.reload(server)
        assert exc_info.value.code == 1
