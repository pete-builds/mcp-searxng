"""Which order should search_deep return its merged results in?

Every strategy below orders the SAME deduplicated set, built by the shipped
client's own shaping and canonical-URL merge from the recorded pages. Only the
order differs, so the pool of relevant results, and therefore the ideal DCG,
is identical across strategies and the numbers are directly comparable.

    A  current (<= 0.5.0): engine_count desc, then SearXNG score summed across pages
    B  native: SearXNG's own order, page 1 first, first sighting wins
    C  best score: max SearXNG score desc (scores restart on every page)
    D  consensus then native: engine_count desc, then SearXNG's order
    E  B's top 10, then the rest by D
    F  B's top 10, then the rest by C   <- shipped

Usage:
    python bench/ranking_experiment.py [--pages 3]
"""

from __future__ import annotations

import argparse
import gzip
import json
import random
import statistics
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE.parent / "src"))
sys.path.insert(0, str(HERE))

from mcp_searxng.clients.searxng import SearxngClient, canonical_url  # noqa: E402
from replay import dcg, is_relevant  # noqa: E402


def merged(pages: list[dict]) -> list[dict]:
    """Shaped results across pages, merged by canonical URL, in first-seen order."""
    client = SearxngClient(url="http://searxng.invalid")
    seen: dict[str, dict] = {}
    order = 0
    for page in pages:
        shaped = client._shape_results(page)
        if not shaped:
            break
        for r in shaped:
            key = canonical_url(r["url"] or "")
            if key in seen:
                e = seen[key]
                e["engines"] = sorted(set(e["engines"]) | set(r["engines"]))
                e["engine_count"] = len(e["engines"])
                e["score_sum"] += r.get("score", 0)
                e["score_max"] = max(e["score_max"], r.get("score", 0))
            else:
                seen[key] = {**r, "first_seen": order,
                             "score_sum": r.get("score", 0), "score_max": r.get("score", 0)}
                order += 1
    return list(seen.values())


STRATEGIES = {
    "A current": lambda rs: sorted(rs, key=lambda r: (-r["engine_count"], -r["score_sum"])),
    "B native": lambda rs: sorted(rs, key=lambda r: r["first_seen"]),
    "C best score": lambda rs: sorted(rs, key=lambda r: (-r["score_max"], r["first_seen"])),
    "D consensus, native": lambda rs: sorted(rs, key=lambda r: (-r["engine_count"], r["first_seen"])),
    # E: SearXNG's order for the top 10, then the remainder by consensus, so
    # the head stays relevance-ordered and the 50-cut keeps corroborated hits.
    "E native head, consensus tail": lambda rs: (
        sorted(rs, key=lambda r: r["first_seen"])[:10]
        + sorted(sorted(rs, key=lambda r: r["first_seen"])[10:],
                 key=lambda r: (-r["engine_count"], r["first_seen"]))
    ),
    # F: SearXNG's order for the top 10, then the remainder by best per-page
    # score, so the strong top of a later page outranks page 1's long tail.
    "F native head, best-score tail": lambda rs: (
        sorted(rs, key=lambda r: r["first_seen"])[:10]
        + sorted(sorted(rs, key=lambda r: r["first_seen"])[10:],
                 key=lambda r: (-r["score_max"], r["first_seen"]))
    ),
}


def metrics(urls: list[str], gold: list[str], pool: int) -> dict:
    rels = [1 if is_relevant(u, gold) else 0 for u in urls]
    first = next((i for i, r in enumerate(rels) if r), None)
    ideal = dcg([1] * min(pool, 10))
    return {
        "success5": 1.0 if any(rels[:5]) else 0.0,
        "mrr10": 1.0 / (first + 1) if first is not None and first < 10 else 0.0,
        "ndcg10": dcg(rels[:10]) / ideal if ideal else 0.0,
        "p10": sum(rels[:10]) / 10,
        # Share of every relevant result in the merged set that survives the
        # default max_results=50 cut. research reads all 50.
        "recall50": (sum(rels[:50]) / pool) if pool else 1.0,
    }


def bootstrap_ci(diffs: list[float], n: int = 10000, seed: int = 7) -> tuple[float, float]:
    rng = random.Random(seed)
    means = sorted(statistics.mean(rng.choices(diffs, k=len(diffs))) for _ in range(n))
    return means[int(0.025 * n)], means[int(0.975 * n)]


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--pages", type=int, default=3)
    ap.add_argument("--fixture", default=str(HERE / "fixtures" / "capture.json.gz"))
    ap.add_argument("--queries", default=str(HERE / "queries.json"))
    args = ap.parse_args()

    with gzip.open(args.fixture, "rt", encoding="utf-8") as fh:
        rec = json.load(fh)
    queries = json.loads(Path(args.queries).read_text())["queries"]

    per: dict[str, dict[str, list[float]]] = {s: {} for s in STRATEGIES}
    for q in queries:
        rs = merged(rec["queries"][q["q"]][: args.pages])
        pool = sum(1 for r in rs if is_relevant(r["url"], q["gold"]))
        for name, order in STRATEGIES.items():
            m = metrics([r["url"] for r in order(rs)], q["gold"], pool)
            for k, v in m.items():
                per[name].setdefault(k, []).append(v)

    print(f"pages={args.pages}  queries={len(queries)}  fixture captured "
          f"{rec['captured_at']}  SearXNG {rec['searxng_version']}\n")
    print("| strategy | Success@5 | MRR@10 | nDCG@10 | P@10 | Recall@50 |")
    print("|---|---|---|---|---|---|")
    for name in STRATEGIES:
        m = per[name]
        print(f"| {name} | {statistics.mean(m['success5']):.3f} | {statistics.mean(m['mrr10']):.3f} | "
              f"{statistics.mean(m['ndcg10']):.3f} | {statistics.mean(m['p10']):.3f} | "
              f"{statistics.mean(m['recall50']):.3f} |")

    for metric in ("ndcg10", "recall50"):
        base = per["A current"][metric]
        print(f"\nPaired {metric} difference vs A, per query (bootstrap 95% CI of the mean):")
        for name in STRATEGIES:
            if name == "A current":
                continue
            diffs = [b - a for a, b in zip(base, per[name][metric])]
            lo, hi = bootstrap_ci(diffs)
            wins = sum(d > 1e-9 for d in diffs)
            losses = sum(d < -1e-9 for d in diffs)
            print(f"  {name:30s} mean {statistics.mean(diffs):+.3f}  CI [{lo:+.3f}, {hi:+.3f}]  "
                  f"better on {wins}, worse on {losses}, tied on {len(diffs) - wins - losses}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
