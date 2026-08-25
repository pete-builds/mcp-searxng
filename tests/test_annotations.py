"""Every tool declares itself read-only, and that claim is checked.

Nine tools, none of which writes anything anywhere. That is worth declaring
rather than leaving to be inferred: an unannotated read-only server and an
unannotated server full of delete tools are indistinguishable in the manifest,
so a client trying to be careful has to be careful about everything -- which in
practice means being careful about nothing.

READ-ONLY IS NOT THE SAME AS HARMLESS, and these hints do not claim it is.
read_url fetches an attacker-influenceable page and the search tools return text
the caller did not write. That risk is handled by the SSRF validation and
redirect checks in clients/, which have their own tests. These hints describe
EFFECTS on the world, and the effect of every tool here is none.
"""

from __future__ import annotations

import asyncio
import os

import pytest

# server.py exits at import when SEARXNG_URL is unset. No request is made here:
# the manifest is built from the registered tools, not from the instance.
os.environ.setdefault("SEARXNG_URL", "http://searxng.invalid:8888")

from mcp_searxng.server import mcp  # noqa: E402

EXPECTED = {
    "search_person", "search", "search_news", "search_tech", "search_deep",
    "search_images", "search_videos", "read_url", "get_engines",
}


@pytest.fixture(scope="module")
def tools():
    """The live manifest, not the source. What a client would receive."""
    return {t.name: t for t in asyncio.run(mcp.list_tools())}


def test_the_expected_nine_are_present(tools):
    """Guards the guard: an empty manifest would pass everything below."""
    assert set(tools) == EXPECTED


def test_every_tool_is_annotated(tools):
    assert sorted(n for n, t in tools.items() if t.annotations is None) == []


def test_every_tool_is_read_only(tools):
    """The whole surface. A write tool added later fails here first.

    That is the point: the failure is a prompt to classify the new tool
    deliberately, not an obstacle to adding one.
    """
    assert sorted(n for n, t in tools.items() if not t.annotations.readOnlyHint) == []


def test_nothing_claims_to_be_destructive(tools):
    assert sorted(n for n, t in tools.items() if t.annotations.destructiveHint) == []


def test_every_tool_declares_an_open_world(tools):
    """An answer can differ between identical calls because the web moved.

    Which is a different thing from the call having changed it, and is why
    these are open-world AND idempotent at the same time.
    """
    closed = sorted(n for n, t in tools.items() if t.annotations.openWorldHint is not True)
    assert closed == []
    assert sorted(n for n, t in tools.items() if not t.annotations.idempotentHint) == []
