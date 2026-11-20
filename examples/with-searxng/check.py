"""Check a running quick-start stack end to end, as an MCP client would.

Runs inside the mcp-searxng container, which already has the client library:

    docker compose exec -T -e TOKEN="$MCP_AUTH_TOKEN" mcp-searxng python - < check.py

Passes when the server lists its tools, a search round-trips through SearXNG's
JSON API (results may be thin on a fresh instance; the provenance block says
so), and the engine list is non-empty.
"""

import asyncio
import json
import os

from fastmcp import Client
from fastmcp.client.auth import BearerAuth

URL = os.environ.get("MCP_URL", "http://127.0.0.1:3702/mcp")


async def main() -> None:
    async with Client(URL, auth=BearerAuth(os.environ["TOKEN"])) as client:
        tools = await client.list_tools()
        assert len(tools) == 9, f"expected 9 tools, got {len(tools)}"
        print(f"tools: {len(tools)}")

        # SearXNG may still be starting: retry for about 30 s.
        for _ in range(15):
            res = await client.call_tool("search", {"query": "searxng"}, raise_on_error=False)
            if not res.is_error:
                break
            print("search not ready yet:", res.content[0].text[:160])
            await asyncio.sleep(2)
        assert not res.is_error, res.content[0].text
        data = json.loads(res.content[0].text)
        prov = data["provenance"]
        print(f"search: {len(data['results'])} results from {prov['engine_count']} engine(s), "
              f"degraded={prov['degraded']}")

        engines = json.loads((await client.call_tool("get_engines", {})).content[0].text)
        assert engines["engines_count"] > 0
        print(f"get_engines: {engines['engines_count']} engines")
    print("OK")


asyncio.run(main())
