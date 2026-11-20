"""Replay a captured SearXNG recording through one version of the server, then score.

Two subcommands:

    run      Import the server from --src, swap its SearXNG transport for the
             recording, call the real tools through an in-memory MCP client, and
             write what the agent would have received to --out.
    compare  Score two `run` outputs against the gold sets in queries.json and
             print a markdown table.

Usage:
    git worktree add /tmp/v050 v0.5.0
    python bench/replay.py run --src /tmp/v050/src --out /tmp/old.json
    python bench/replay.py run --src src --out /tmp/new.json
    python bench/replay.py compare /tmp/old.json /tmp/new.json

The tools are called through fastmcp.Client over the in-memory transport, so the
text measured is exactly the text an MCP client receives, formatter included.
Only the HTTP call to SearXNG is replaced; ranking, dedup, provenance and
formatting all run as shipped.
"""

from __future__ import annotations

import argparse
import asyncio
import gzip
import json
import math
import os
import statistics
import sys
from pathlib import Path
from urllib.parse import parse_qsl, urlencode, urlsplit

HERE = Path(__file__).resolve().parent
DEFAULT_FIXTURE = HERE / "fixtures" / "capture.json.gz"

#: Calls made per query. research uses search_deep with pages 3-5, max 50.
CALLS = [
    ("search", {"max_results": 10}),
    ("search_deep", {"pages": 3, "max_results": 50}),
    ("search_deep", {"pages": 5, "max_results": 50}),
]


# --------------------------------------------------------------------------
# run
# --------------------------------------------------------------------------


async def _run(src: str, fixture: Path, out: Path) -> None:
    os.environ.setdefault("SEARXNG_URL", "http://searxng.invalid:8888")
    os.environ.setdefault("MCP_AUTH_TOKEN", "bench-token-not-a-secret")
    os.environ.setdefault("LOG_LEVEL", "WARNING")
    sys.path.insert(0, str(Path(src).resolve()))

    import fastmcp

    from mcp_searxng import server

    loaded = Path(server.__file__).resolve()
    assert str(loaded).startswith(str(Path(src).resolve())), f"imported {loaded}, not {src}"

    with gzip.open(fixture, "rt", encoding="utf-8") as fh:
        rec = json.load(fh)

    calls_made: list[tuple[str, int]] = []

    async def fake_get(path, params=None):
        assert path == "/search", path
        pages = rec["queries"].get(params["q"])
        if pages is None:
            raise KeyError(f"query not in recording: {params['q']!r}")
        pageno = int(params.get("pageno", 1))
        calls_made.append((params["q"], pageno))
        if pageno > len(pages):
            return {"results": [], "unresponsive_engines": []}
        return json.loads(json.dumps(pages[pageno - 1]))  # fresh copy per call

    async def no_wait(*_a, **_k):
        return None

    server.searxng._get = fake_get
    if hasattr(server.searxng, "_rate_limit"):
        server.searxng._rate_limit = no_wait

    queries = json.loads((HERE / "queries.json").read_text())["queries"]
    result: dict = {"src": src, "manifest": None, "runs": []}

    async with fastmcp.Client(server.mcp) as client:
        tools = await client.list_tools()
        manifest = [
            {"name": t.name, "description": t.description, "inputSchema": t.inputSchema}
            for t in tools
        ]
        result["manifest"] = {
            "tools": len(manifest),
            "chars": len(json.dumps(manifest, separators=(",", ":"))),
        }
        for q in queries:
            for tool, kwargs in CALLS:
                calls_made.clear()
                res = await client.call_tool(tool, {"query": q["q"], **kwargs})
                text = res.content[0].text
                data = json.loads(text)
                result["runs"].append({
                    "id": q["id"],
                    "tool": tool,
                    "args": kwargs,
                    "chars": len(text),
                    "urls": [r.get("url") for r in data.get("results", [])],
                    "upstream_calls": len(calls_made),
                    "degraded": (data.get("provenance") or {}).get("degraded"),
                })

    out.write_text(json.dumps(result, indent=1))
    print(f"wrote {out} ({len(result['runs'])} runs, manifest {result['manifest']})")


# --------------------------------------------------------------------------
# compare
# --------------------------------------------------------------------------

TRACKING = {"fbclid", "gclid", "dclid", "msclkid", "mc_cid", "mc_eid", "ref_src", "igshid"}


def canon(url: str) -> str:
    """The benchmark's own idea of 'the same page', independent of the server's.

    Ignores scheme, a leading www., a default port, a trailing slash, the
    fragment, utm_* and common click-tracking parameters, and query order.
    """
    p = urlsplit(url or "")
    host = (p.hostname or "").lower()
    if host.startswith("www."):
        host = host[4:]
    port = f":{p.port}" if p.port and p.port not in (80, 443) else ""
    path = p.path.rstrip("/") or "/"
    qs = sorted(
        (k, v) for k, v in parse_qsl(p.query, keep_blank_values=True)
        if not k.lower().startswith("utm_") and k.lower() not in TRACKING
    )
    return f"{host}{port}{path}" + (f"?{urlencode(qs)}" if qs else "")


def is_relevant(url: str, gold: list[str]) -> bool:
    p = urlsplit(url or "")
    host = (p.hostname or "").lower()
    if host.startswith("www."):
        host = host[4:]
    for g in gold:
        ghost, _, gpath = g.partition("/")
        ghost = ghost.lower()
        if host == ghost or host.endswith("." + ghost):
            if not gpath or p.path.lstrip("/").lower().startswith(gpath.lower()):
                return True
    return False


def dcg(rels: list[int]) -> float:
    return sum(r / math.log2(i + 2) for i, r in enumerate(rels))


def score_run(urls: list[str], gold: list[str], pool_relevant: int, k: int = 10) -> dict:
    rels = [1 if is_relevant(u, gold) else 0 for u in urls]
    first = next((i for i, r in enumerate(rels) if r), None)
    ideal = dcg([1] * min(pool_relevant, k))
    keys = [canon(u) for u in urls]
    return {
        "success5": 1.0 if any(rels[:5]) else 0.0,
        "mrr10": (1.0 / (first + 1)) if first is not None and first < k else 0.0,
        "ndcg10": (dcg(rels[:k]) / ideal) if ideal else 0.0,
        "dupes": len(keys) - len(set(keys)),
        "unique": len(set(keys)),
        "n": len(urls),
        "relevant_unique": len({c for c, r in zip(keys, rels) if r}),
    }


def _compare(old_path: Path, new_path: Path) -> None:
    old = json.loads(old_path.read_text())
    new = json.loads(new_path.read_text())
    gold = {q["id"]: q["gold"] for q in json.loads((HERE / "queries.json").read_text())["queries"]}

    def key(r):
        return (r["id"], r["tool"], json.dumps(r["args"], sort_keys=True))

    old_runs = {key(r): r for r in old["runs"]}
    new_runs = {key(r): r for r in new["runs"]}

    print(f"Manifest: old {old['manifest']}  new {new['manifest']}\n")
    print("| call | metric | old | new |")
    print("|---|---|---|---|")
    for tool, kwargs in CALLS:
        label = f"{tool} {kwargs}"
        rows: dict[str, list[tuple[float, float]]] = {}
        chars: list[tuple[int, int]] = []
        for qid, g in gold.items():
            k = (qid, tool, json.dumps(kwargs, sort_keys=True))
            if k not in old_runs or k not in new_runs:
                continue
            o, n = old_runs[k], new_runs[k]
            # Pooled ideal: every relevant distinct page either version surfaced.
            pool = {canon(u) for u in o["urls"] + n["urls"] if is_relevant(u, g)}
            so = score_run(o["urls"], g, len(pool))
            sn = score_run(n["urls"], g, len(pool))
            for m in so:
                rows.setdefault(m, []).append((so[m], sn[m]))
            chars.append((o["chars"], n["chars"]))
        for m in ("success5", "mrr10", "ndcg10"):
            vals = rows[m]
            print(f"| {label} | {m} (mean, n={len(vals)}) | "
                  f"{statistics.mean(v[0] for v in vals):.3f} | {statistics.mean(v[1] for v in vals):.3f} |")
        for m in ("dupes", "unique", "relevant_unique", "n"):
            vals = rows[m]
            print(f"| {label} | {m} (total) | {sum(v[0] for v in vals):.0f} | {sum(v[1] for v in vals):.0f} |")
        oc, nc = sum(c[0] for c in chars), sum(c[1] for c in chars)
        print(f"| {label} | response chars (total) | {oc} | {nc} |")
        print(f"| {label} | est. tokens per call (chars/4, mean) | "
              f"{oc / 4 / len(chars):.0f} | {nc / 4 / len(chars):.0f} |")


def main() -> int:
    ap = argparse.ArgumentParser()
    sub = ap.add_subparsers(dest="cmd", required=True)
    r = sub.add_parser("run")
    r.add_argument("--src", required=True)
    r.add_argument("--out", required=True)
    r.add_argument("--fixture", default=str(DEFAULT_FIXTURE))
    c = sub.add_parser("compare")
    c.add_argument("old")
    c.add_argument("new")
    args = ap.parse_args()
    if args.cmd == "run":
        asyncio.run(_run(args.src, Path(args.fixture), Path(args.out)))
    else:
        _compare(Path(args.old), Path(args.new))
    return 0


if __name__ == "__main__":
    sys.exit(main())
