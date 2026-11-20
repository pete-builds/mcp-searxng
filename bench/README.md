# Benchmarks

Reproducible measurements of this server, version against version. Nothing
here compares against other SearXNG MCP servers: that would need their code run
on the same recording, which has not been done.

## What is measured, and how

| Script | Question | Data |
|---|---|---|
| `capture.py` | Records SearXNG's raw answers once | live SearXNG, pages 1 to 5 per query |
| `replay.py run` / `compare` | Same recording through two versions: relevance, duplicates, response size | the recording |
| `ranking_experiment.py` | Which order should `search_deep` return merged results in? | the recording |
| `live.py` | Latency, a concurrent burst, read_url outcomes, peak memory | live, real HTTP server, real MCP client |
| `demo.py` | A research workflow, as a transcript | live |

**Why a recording.** Live results change from minute to minute and engines are
suspended mid-run, so two live runs mostly measure the web moving. `capture.py`
stores what SearXNG returned (with its version and engine lineup) and
`replay.py` feeds that to each version through a real in-memory MCP client, so
ranking, dedup, provenance and the output formatter all run as shipped.

**Relevance.** Each query has a small gold set of domains or URL prefixes that
a practitioner expects on page one (the primary source, its docs, its repo),
written before any ranking change (`queries.json`, first commit of this
directory). A result is relevant when its host matches a gold entry. Metrics:
Success@5, MRR@10, nDCG@10 with binary gains and a pooled ideal, P@10, and
Recall@50 (the share of relevant results that survive the default
`max_results=50` cut). A second set, `holdout.json`, was written after the
ranking strategy was chosen and before it was ever run, to check the choice
was not fitted to the first set.

**Tokens** are estimated as characters / 4. They are labelled as estimates
everywhere; no tokenizer was run.

## Reproduce

```bash
git worktree add /tmp/v050 v0.5.0
python bench/replay.py run --src /tmp/v050/src --out /tmp/old.json
python bench/replay.py run --src src --out /tmp/new.json
python bench/replay.py compare /tmp/old.json /tmp/new.json
python bench/ranking_experiment.py --pages 3
python bench/ranking_experiment.py --pages 3 \
  --fixture bench/fixtures/holdout.json.gz --queries bench/holdout.json
```

Live (needs your own SearXNG):

```bash
SEARXNG_URL=http://your-searxng:8888 python bench/capture.py      # new recording
SEARXNG_URL=http://your-searxng:8888 python bench/live.py --src src --out live.json
```

## Results, 2026-11-20

Recording: SearXNG 2026.11.15+2d500cb6b, 24 queries and a 14-query holdout,
5 pages each. Engines that answered: bing, duckduckgo, mwmbl, yahoo on page 1;
mostly duckduckgo and yahoo on later pages; google and startpage suspended.
That is a degraded but typical self-hosted lineup, and the numbers below are
for it, not for every instance.

### search_deep ordering (`ranking_experiment.py`, 3 pages)

| strategy | nDCG@10 | MRR@10 | P@10 | Recall@50 |
|---|---|---|---|---|
| 0.5.0: engine_count, then summed score | 0.416 | 0.684 | 0.342 | 0.896 |
| SearXNG's own order | 0.473 | 0.731 | 0.392 | 0.832 |
| **shipped: SearXNG's order for the top 10, best per-page score after** | **0.473** | **0.731** | **0.392** | **0.896** |
| holdout, 0.5.0 | 0.427 | 0.769 | 0.314 | 0.850 |
| **holdout, shipped** | **0.492** | **0.848** | **0.364** | **0.850** |

Paired nDCG@10 difference, shipped minus 0.5.0, bootstrap 95% CI of the mean:
+0.057 [+0.011, +0.103] (better on 18 of 24 queries, worse on 6), holdout
+0.065 [+0.010, +0.122] (better on 10 of 14, worse on 2). At 5 pages the
holdout gain is +0.048 [−0.004, +0.099]: positive, but the interval touches
zero. Six orderings were tried on the first set, so treat the first-set
numbers as optimistic and the holdout as the honest estimate.

### Through the MCP layer (`replay.py compare`, 24 queries)

| call | metric | 0.5.0 | 0.6.0 |
|---|---|---|---|
| search_deep pages=3, max 50 | duplicates (same page, different URL) | 6 | 0 |
| | relevant results in the 50 returned | 334 | 336 |
| | est. tokens per call | 6,625 | 5,410 |
| search_deep pages=5, max 50 | duplicates | 5 | 0 |
| | relevant results in the 50 returned | 338 | 346 |
| | est. tokens per call | 7,147 | 5,798 |
| search, max 10 | est. tokens per call (same results) | 1,519 | 1,225 |
| tool manifest | characters (9 tools; 4 new optional parameters) | 8,547 | 8,672 |

### Live (`live.py`, one run each, same minute, real HTTP + bearer token)

| | 0.5.0 | 0.6.0 |
|---|---|---|
| search, 12 sequential, median / p95 | 0.98 s / 1.18 s | 0.94 s / 1.18 s |
| search_deep pages=3, 3 runs, median | 2.95 s | 2.92 s |
| 8 concurrent searches, wall time | 1.9 s (all sent at once) | 8.2 s (spaced 1 s apart, `SEARXNG_MIN_INTERVAL`) |
| read_url, PDF (arxiv) | 2.1 M chars, 88% printable, as "markdown" | `UNSUPPORTED_CONTENT_TYPE` in 0.08 s |
| read_url, 10 MB binary | downloaded in full, then "too large" | refused from headers in 0.35 s |
| peak server memory | 219 MB | 169 MB |
| read_url on 6 HTML/text pages, A/B alternating, 18 fetches each | median 0.102 s | median 0.103 s |

The burst row is the rate limit now doing what it always claimed to: 0.5.0's
limiter let concurrent callers through together. Set `SEARXNG_MIN_INTERVAL=0`
to trade that spacing for speed.

## Limits

- One SearXNG instance, one day, a degraded engine lineup. Rankings on an
  instance with Google and Startpage answering may differ.
- Gold sets are small, domain-level, and written by one person. They measure
  whether authoritative sources surface near the top, not fine-grained relevance.
- 24 + 14 queries. The confidence intervals above are wide for that reason.
- Live latency is dominated by SearXNG and its engines and by the rate limit.
  It is a regression check, not a speed claim.
- Memory is one run's peak, not a controlled measurement.
