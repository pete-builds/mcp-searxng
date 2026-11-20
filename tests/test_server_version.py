"""A client sees this server's own release in serverInfo, not FastMCP's.

Without an explicit ``version=`` FastMCP fills serverInfo.version with its own
package version. Under the 0.5.0 code a client's initialize came back as
"SearXNG Search" version "4.2.2", which is FastMCP, so a user debugging an
install could not tell which release of this server they were running.

The expected value is read from pyproject.toml, not from package metadata,
because the server itself reads metadata: comparing it with itself would pass
whatever it said. This boots the real entry point, the way the container does,
and reads the version off the wire.
"""

from __future__ import annotations

import http.client
import json
import tomllib
from pathlib import Path

from test_lan_host import INITIALIZE, TOKEN, _serve

PYPROJECT = Path(__file__).resolve().parent.parent / "pyproject.toml"


def _server_info(port: int) -> dict:
    conn = http.client.HTTPConnection("127.0.0.1", port, timeout=10)
    try:
        conn.request(
            "POST",
            "/mcp",
            body=INITIALIZE,
            headers={
                "Content-Type": "application/json",
                "Accept": "application/json, text/event-stream",
                "Authorization": f"Bearer {TOKEN}",
            },
        )
        resp = conn.getresponse()
        body = resp.read().decode()
        assert resp.status == 200, f"initialize returned {resp.status}: {body[:500]}"
    finally:
        conn.close()
    # Streamable HTTP may answer as SSE ("data: {...}") or as plain JSON.
    payload = next(
        (line[len("data:"):].strip() for line in body.splitlines() if line.startswith("data:")),
        body,
    )
    return json.loads(payload)["result"]["serverInfo"]


def test_server_info_reports_the_pyproject_version():
    expected = tomllib.loads(PYPROJECT.read_text())["project"]["version"]
    with _serve() as port:
        info = _server_info(port)
    assert info["name"] == "SearXNG Search"
    assert info["version"] == expected, (
        f"serverInfo.version is {info['version']!r}, pyproject says {expected!r}"
    )
