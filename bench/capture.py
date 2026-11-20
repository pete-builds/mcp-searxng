"""Capture raw SearXNG responses once, so every version is scored on the same data.

Live search results change from minute to minute and engines get suspended
mid-run, so comparing two versions by running each one live would mostly
measure the web moving. This script records pages 1..N of every benchmark
query exactly as SearXNG returned them, and replay.py feeds that recording to
each version.

Usage:
    SEARXNG_URL=http://your-searxng:8888 python bench/capture.py [--pages 5] [--delay 2.0]

Writes bench/fixtures/capture.json.gz. The instance URL is NOT recorded, only
its version and engine lineup.
"""

from __future__ import annotations

import argparse
import datetime as dt
import gzip
import json
import os
import sys
import time
from pathlib import Path

import httpx

HERE = Path(__file__).resolve().parent
KEEP = ("title", "url", "content", "engine", "engines", "score", "publishedDate", "thumbnail")


def trim(page: dict) -> dict:
    """Keep only the fields the client reads, to keep the fixture small."""
    return {
        "query": page.get("query"),
        "number_of_results": page.get("number_of_results"),
        "unresponsive_engines": page.get("unresponsive_engines") or [],
        "suggestions": page.get("suggestions") or [],
        "corrections": page.get("corrections") or [],
        "infoboxes": page.get("infoboxes") or [],
        "results": [{k: r[k] for k in KEEP if k in r} for r in page.get("results", [])],
    }


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--pages", type=int, default=5)
    ap.add_argument("--delay", type=float, default=2.0, help="seconds between requests")
    ap.add_argument("--out", default=str(HERE / "fixtures" / "capture.json.gz"))
    args = ap.parse_args()

    base = os.environ.get("SEARXNG_URL", "").rstrip("/")
    if not base:
        print("SEARXNG_URL is not set", file=sys.stderr)
        return 2

    queries = json.loads((HERE / "queries.json").read_text())["queries"]
    client = httpx.Client(timeout=30)

    cfg = client.get(f"{base}/config").json()
    general = sorted(
        e["name"] for e in cfg.get("engines", [])
        if e.get("enabled", True) and "general" in e.get("categories", [])
    )
    out: dict = {
        "captured_at": dt.datetime.now(dt.UTC).isoformat(timespec="seconds"),
        "searxng_version": cfg.get("version"),
        "general_engines_enabled": general,
        "params": {"categories": "general", "language": "en", "safesearch": 0},
        "pages_requested": args.pages,
        "queries": {},
    }

    for q in queries:
        pages = []
        for pageno in range(1, args.pages + 1):
            time.sleep(args.delay)
            resp = client.get(
                f"{base}/search",
                params={
                    "q": q["q"], "format": "json", "categories": "general",
                    "language": "en", "pageno": pageno, "safesearch": 0,
                },
            )
            resp.raise_for_status()
            page = trim(resp.json())
            pages.append(page)
            engines = sorted({e for r in page["results"] for e in (r.get("engines") or [])})
            print(f"{q['id']:20s} p{pageno} results={len(page['results']):3d} "
                  f"engines={','.join(engines)} unresponsive={len(page['unresponsive_engines'])}")
            if not page["results"]:
                break
        out["queries"][q["q"]] = pages

    Path(args.out).parent.mkdir(parents=True, exist_ok=True)
    with gzip.open(args.out, "wt", encoding="utf-8") as fh:
        json.dump(out, fh, separators=(",", ":"))
    print(f"wrote {args.out}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
