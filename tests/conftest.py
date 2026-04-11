"""Shared test fixtures for mcp-searxng."""

import pytest

from clients.searxng import SearxngClient


@pytest.fixture
def searxng_url():
    """Base URL for the SearXNG instance."""
    return "http://searxng.test:8080"


@pytest.fixture
def client(searxng_url):
    """Create a SearxngClient with a test URL."""
    return SearxngClient(searxng_url)


@pytest.fixture
def sample_search_response():
    """Realistic SearXNG search API response."""
    return {
        "results": [
            {
                "title": "Example Result",
                "url": "https://example.com/page",
                "content": "This is a sample search result content.",
                "engine": "google",
                "category": "general",
            },
            {
                "title": "Another Result",
                "url": "https://example.com/other",
                "content": "Another sample result from a different source.",
                "engine": "duckduckgo",
                "category": "general",
            },
        ],
        "number_of_results": 2,
        "query": "test query",
    }


@pytest.fixture
def sample_news_response():
    """Realistic SearXNG news search response."""
    return {
        "results": [
            {
                "title": "Breaking News",
                "url": "https://news.example.com/story",
                "content": "A news story about something important.",
                "engine": "google news",
                "category": "news",
                "publishedDate": "2026-04-10T12:00:00Z",
            },
        ],
        "number_of_results": 1,
        "query": "news query",
    }
