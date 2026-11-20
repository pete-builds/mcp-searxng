"""Startup probe of SearXNG, and the registry metadata that must track releases."""

import json
import os
import sys
import tomllib
from pathlib import Path

import httpx
import pytest
import respx

os.environ.setdefault("SEARXNG_URL", "http://searxng.invalid:8888")
os.environ.setdefault("MCP_AUTH_TOKEN", "test-token-not-a-secret")
ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "src"))

from mcp_searxng import server  # noqa: E402

URL = "http://searxng.probe:8888"


class TestStartupProbe:
    """Status codes measured against a real SearXNG (2026.11.15): an empty JSON
    query is a 400 JSON error when the format is on, a 403 HTML page when off."""

    @respx.mock
    def test_json_enabled_is_quiet(self) -> None:
        respx.get(f"{URL}/search").mock(
            return_value=httpx.Response(400, json={"error": "search error"})
        )
        assert server.probe_searxng(URL) is None

    @respx.mock
    def test_json_disabled_names_the_setting(self) -> None:
        respx.get(f"{URL}/search").mock(
            return_value=httpx.Response(403, headers={"content-type": "text/html"}, content=b"no")
        )
        assert "search.formats" in server.probe_searxng(URL)

    @respx.mock
    def test_unreachable_warns_but_never_raises(self) -> None:
        respx.get(f"{URL}/search").mock(side_effect=httpx.ConnectError("refused"))
        assert "not reachable" in server.probe_searxng(URL)

    @respx.mock
    def test_wrong_service_is_caught(self) -> None:
        respx.get(f"{URL}/search").mock(
            return_value=httpx.Response(200, headers={"content-type": "text/html"}, content=b"<h1>")
        )
        assert "not JSON" in server.probe_searxng(URL)

    @respx.mock
    def test_probe_sends_an_empty_query(self) -> None:
        """An empty q is what keeps the probe off the engines entirely."""
        route = respx.get(f"{URL}/search").mock(return_value=httpx.Response(400, json={}))
        server.probe_searxng(URL)
        assert route.calls.last.request.url.params["q"] == ""


@pytest.fixture(scope="module")
def meta():
    return json.loads((ROOT / "server.json").read_text())


class TestRegistryMetadata:
    def test_version_tracks_pyproject(self, meta) -> None:
        version = tomllib.loads((ROOT / "pyproject.toml").read_text())["project"]["version"]
        assert meta["version"] == version
        assert meta["packages"][0]["identifier"].endswith(f":{version}")

    def test_image_label_matches_registry_name(self, meta) -> None:
        """The registry verifies ownership by this label; a mismatch blocks listing."""
        label = f'io.modelcontextprotocol.server.name="{meta["name"]}"'
        assert label in (ROOT / "Dockerfile").read_text()

    def test_description_fits_the_registry_limit(self, meta) -> None:
        assert len(meta["description"]) <= 100


def test_install_docs_pin_the_current_release() -> None:
    """Every copy-paste install line names the version in pyproject.toml.

    A release that bumps pyproject but not these leaves new users installing
    the previous release from the README while reading about this one.
    """
    import re

    version = tomllib.loads((ROOT / "pyproject.toml").read_text())["project"]["version"]
    for rel in ("README.md", "examples/with-searxng/docker-compose.yml", "docker-compose.yml"):
        text = (ROOT / rel).read_text()
        pins = set(re.findall(r"mcp-searxng(?::|@v)(\d+\.\d+\.\d+)", text))
        assert pins, f"{rel}: no version pin found (did the pattern change?)"
        assert pins == {version}, f"{rel} pins {sorted(pins)}, pyproject is {version}"
