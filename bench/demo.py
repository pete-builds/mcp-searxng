"""A research workflow an agent would actually run, printed as a short transcript.

    1. search_deep for sources, restricted to primary documentation domains
    2. read_url on the top three
    3. search_news for anything recent
    4. read_url on a PDF and on a cloud-metadata address, to show the refusals

Prints what an agent would see, trimmed: provenance, the first results, the
reads. Run it against a server you started yourself:

    TOKEN=... MCP_URL=http://127.0.0.1:3702/mcp python bench/demo.py
"""

from __future__ import annotations

import asyncio
import json
import os

from fastmcp import Client
from fastmcp.client.auth import BearerAuth


def show(label: str, text: str) -> dict:
    data = json.loads(text)
    print(f"\n### {label}")
    return data


async def main() -> None:
    url = os.environ.get("MCP_URL", "http://127.0.0.1:3702/mcp")
    async with Client(url, auth=BearerAuth(os.environ["TOKEN"])) as c:
        r = await c.call_tool("search_deep", {
            "query": "HTTP/3 connection migration",
            "pages": 3, "max_results": 8,
            "include_domains": "rfc-editor.org,ietf.org,developer.mozilla.org,cloudflare.com",
        })
        d = show("search_deep, primary sources only", r.content[0].text)
        p = d["provenance"]
        print(f"pages fetched {d['pages_fetched']}/{d['pages_requested']}; "
              f"{len(d['results'])} results; engines {p['engines_contributing']}; "
              f"degraded={p['degraded']}")
        for x in d["results"][:5]:
            print(f"  [{x['engine_count']} engines] {x['title'][:70]}\n      {x['url']}")

        for x in d["results"][:3]:
            r = await c.call_tool("read_url", {"url": x["url"], "max_chars": 400})
            rd = json.loads(r.content[0].text)
            if "error" in rd:
                print(f"\nread_url {x['url']}\n  -> {rd['code']}: {rd['error'][:90]}")
            else:
                snippet = " ".join(rd["markdown"].split())[:160]
                print(f"\nread_url {rd['url']}\n  -> {rd.get('total_length', rd['length'])} chars; "
                      f"\"{snippet}...\"")

        r = await c.call_tool("search_news", {"query": "HTTP/3 adoption", "max_results": 3})
        d = show("search_news, past week", r.content[0].text)
        print(f"degraded={d['provenance']['degraded']}  "
              f"engines {d['provenance']['engines_contributing']}")
        for x in d["results"]:
            print(f"  {x.get('published_date', '')[:10]}  {x['title'][:70]}")

        print("\n### refusals")
        for u in ("https://arxiv.org/pdf/1706.03762", "http://169.254.169.254/latest/meta-data/"):
            rd = json.loads((await c.call_tool("read_url", {"url": u})).content[0].text)
            print(f"  {u}\n    -> {rd['code']}")


asyncio.run(main())
