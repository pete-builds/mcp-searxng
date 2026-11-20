"""Live benchmark: one version, run as a real HTTP server, driven by a real MCP client.

Starts the server from --src on 127.0.0.1 with a bearer token and the result
cache off, connects with fastmcp.Client over streamable HTTP, and measures:

- latency of search (N distinct queries, sequential) and search_deep (pages=3)
- a burst of concurrent distinct searches: wall time, per-call latency, errors
- read_url over a fixed URL list: outcome and latency per URL
- the server process's peak resident memory (VmHWM, Linux only)

Live numbers depend on the SearXNG instance and on which engines are answering
at that minute, so every report records the engine lineup it ran against.
Latency here is mostly SearXNG and its engines; it is a regression check, not a
claim that the server is faster.

Usage:
    SEARXNG_URL=http://your-searxng:8888 python bench/live.py --src src --out live-new.json
"""

from __future__ import annotations

import argparse
import asyncio
import json
import os
import secrets
import socket
import statistics
import subprocess
import sys
import time
from pathlib import Path

import httpx

QUERIES = [
    "rust async runtime comparison", "postgres vacuum tuning", "ipv6 privacy extensions",
    "zfs scrub schedule", "wireguard mtu", "sqlite full text search", "css container queries",
    "python typing protocol", "go generics constraints", "terraform state locking",
    "prometheus histogram quantile", "redis streams consumer groups",
]
BURST = [
    "nftables sets", "btrfs snapshots", "caddy reverse proxy", "jq recursive descent",
    "systemd socket activation", "tmux copy mode", "ripgrep ignore files", "fzf preview",
]
DEEP = ["kubernetes operators", "webassembly component model", "home lab backup strategy"]
READ_URLS = [
    "https://en.wikipedia.org/wiki/Model_Context_Protocol",
    "https://docs.python.org/3/library/asyncio-task.html",
    "https://www.rfc-editor.org/rfc/rfc9114.html",
    "https://github.com/searxng/searxng",
    "https://docs.searxng.org/admin/settings/settings_search.html",
    "https://www.kernel.org/",
    "https://raw.githubusercontent.com/searxng/searxng/master/README.rst",
    "https://nominatim.openstreetmap.org/search?q=Ithaca%2C+NY&format=json&limit=1",
    "https://arxiv.org/pdf/1706.03762",
    "https://upload.wikimedia.org/wikipedia/commons/a/a9/Example.jpg",
    "https://proof.ovh.net/files/10Mb.dat",
    "https://httpbin.org/status/404",
    "http://169.254.169.254/latest/meta-data/",
]


def free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


def peak_rss_kb(pid: int) -> int | None:
    try:
        for line in Path(f"/proc/{pid}/status").read_text().splitlines():
            if line.startswith("VmHWM:"):
                return int(line.split()[1])
    except OSError:
        return None
    return None


def pct(values: list[float], q: float) -> float:
    s = sorted(values)
    return s[min(len(s) - 1, round(q * (len(s) - 1)))]


def summarize(lat: list[float]) -> dict:
    return {"n": len(lat), "median_s": round(statistics.median(lat), 3),
            "p95_s": round(pct(lat, 0.95), 3), "max_s": round(max(lat), 3)}


async def timed(client, tool: str, args: dict) -> tuple[float, object]:
    t0 = time.monotonic()
    try:
        res = await client.call_tool(tool, args, raise_on_error=False)
    except Exception as e:  # transport-level failure
        return time.monotonic() - t0, e
    return time.monotonic() - t0, res


def outcome(res) -> str:
    if isinstance(res, Exception):
        return f"transport error: {type(res).__name__}"
    if res.is_error:
        return "tool error: " + res.content[0].text[:80]
    data = json.loads(res.content[0].text)
    if "error" in data:
        return f"error code={data.get('code')} {data['error'][:60]}"
    if "markdown" in data:
        md = data["markdown"]
        printable = sum(c.isprintable() or c in "\n\t" for c in md[:2000]) / max(1, min(len(md), 2000))
        return f"ok {data['extraction']} {data['length']} chars, {printable:.0%} printable"
    return f"ok {len(data.get('results', []))} results"


async def run(src: str, out: Path) -> None:
    searxng = os.environ["SEARXNG_URL"]
    token = secrets.token_urlsafe(24)
    port = free_port()
    env = {
        **os.environ, "SEARXNG_URL": searxng, "MCP_AUTH_TOKEN": token,
        "FASTMCP_HOST": "127.0.0.1", "FASTMCP_PORT": str(port),
        "SEARXNG_CACHE_TTL": "0", "LOG_LEVEL": "WARNING",
        "FASTMCP_CHECK_FOR_UPDATES": "off",
    }
    boot = f"import sys; sys.path.insert(0, {str(Path(src).resolve())!r}); " \
           "from mcp_searxng.server import main; main()"
    proc = subprocess.Popen([sys.executable, "-c", boot], env=env,
                            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    url = f"http://127.0.0.1:{port}/mcp"
    report: dict = {"src": src}
    try:
        for _ in range(100):
            try:
                httpx.get(f"http://127.0.0.1:{port}/", timeout=0.5)
                break
            except httpx.TransportError:
                await asyncio.sleep(0.1)

        cfg = httpx.get(f"{searxng.rstrip('/')}/config", timeout=10).json()
        report["searxng_version"] = cfg.get("version")

        from fastmcp import Client
        from fastmcp.client.auth import BearerAuth

        async with Client(url, auth=BearerAuth(token)) as client:
            # Unauthenticated control: the server must refuse a client with no token.
            async with httpx.AsyncClient() as raw:
                r = await raw.post(url, json={})
                report["no_token_status"] = r.status_code

            lat, engines, degraded = [], set(), 0
            for q in QUERIES:
                dt, res = await timed(client, "search", {"query": q})
                lat.append(dt)
                if not isinstance(res, Exception) and not res.is_error:
                    prov = json.loads(res.content[0].text)["provenance"]
                    engines.update(prov["engines_contributing"])
                    degraded += bool(prov["degraded"])
            report["search"] = {**summarize(lat), "engines_seen": sorted(engines),
                                "degraded_responses": degraded}

            lat = []
            for q in DEEP:
                dt, _ = await timed(client, "search_deep", {"query": q, "pages": 3})
                lat.append(dt)
            report["search_deep_pages3"] = summarize(lat)

            t0 = time.monotonic()
            results = await asyncio.gather(*(timed(client, "search", {"query": q}) for q in BURST))
            report["burst"] = {
                "concurrent": len(BURST),
                "wall_s": round(time.monotonic() - t0, 3),
                **summarize([dt for dt, _ in results]),
                "errors": sum(isinstance(r, Exception) or r.is_error for _, r in results),
            }

            reads = []
            for u in READ_URLS:
                dt, res = await timed(client, "read_url", {"url": u})
                reads.append({"url": u, "s": round(dt, 2), "outcome": outcome(res)})
            report["read_url"] = reads
        report["peak_rss_mb"] = round((peak_rss_kb(proc.pid) or 0) / 1024, 1)
    finally:
        proc.terminate()
        proc.wait(timeout=10)
    out.write_text(json.dumps(report, indent=1))
    print(json.dumps(report, indent=1))


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--src", required=True)
    ap.add_argument("--out", required=True)
    args = ap.parse_args()
    if not os.environ.get("SEARXNG_URL"):
        print("SEARXNG_URL is not set", file=sys.stderr)
        return 2
    asyncio.run(run(args.src, Path(args.out)))
    return 0


if __name__ == "__main__":
    sys.exit(main())
