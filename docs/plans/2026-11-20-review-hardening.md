# mcp-searxng: senior review, benchmark, and P0/P1 improvements

## Context

Pete asked for a critical engineering review of `pete-builds/mcp-searxng` (v0.5.0, deployed on nix1 :3702, auth live), a source-level comparison against competing SearXNG MCP servers, a reproducible benchmark, and implementation of the highest-value low-risk fixes. Constraints from his brief: keep tool contracts, no new runtime dependencies without cause, tests for every change, no invented numbers, and **no push, PR, release, or registry submission without his approval**.

Work happens in a fresh clone in the scratchpad (the submodule is not checked out on this box), on branch `review/2026-11-hardening`. The repo is small: ~1,200 lines of source across `server.py`, `clients/searxng.py`, `clients/reader.py`, `clients/ssrf.py`, with 60 hermetic tests (respx) plus a live `tests/smoke.py`.

Known consumers whose contracts must not break (checked in `.claude/skills/`):
- **research / research-verify**: `read_url` failures arrive as an `error` field inside a normal result; `search_deep` with `pages 3-5, max_results 50`; reads `provenance` and `engine_count`.
- **ridemap**: uses `read_url` to fetch **raw JSON** from Nominatim/Overpass, so JSON passthrough and the no-truncation default (`max_chars=0`) must stay.
- `tests/test_provenance.py::test_rendered_description_mentions_provenance`: tool descriptions must still mention provenance.

## Phase 1 audit: findings (evidence-backed)

### Confirmed defects

| # | Finding | Evidence |
|---|---|---|
| D1 | **read_url buffers the entire body before the size check.** `max_bytes` (5 MB) is checked on `resp.content` after the full download, so a multi-GB response is held in memory and can OOM the container that serves every session. | `clients/reader.py:108-113` (non-streaming `client.get`) |
| D2 | **No total deadline on read_url.** httpx `timeout=20` is per phase and per read, so a server that drips a byte every 19s holds the request indefinitely; 6 redirect hops can stack to minutes. | `clients/reader.py:42-46` |
| D3 | **Blocking DNS on the event loop.** The SSRF guard calls `socket.getaddrinfo` synchronously inside an async tool, so one slow resolver lookup stalls every session on the server. | `clients/ssrf.py:111` |
| D4 | **Rate limiter is not concurrency-safe.** Measured: 5 concurrent calls through the verbatim `_rate_limit` fired with gaps of `[0.0, 0.0, 0.0, 0.0]` s. Under HTTP with several agents, the 1 s spacing that protects engines from IP blocks does not hold. Identical concurrent queries also all go upstream (no in-flight coalescing). | `clients/searxng.py:48-54`, scratchpad probe |
| D5 | **Search tools surface raw exceptions.** No try/except: SearXNG's common `403` (json format disabled) or `429` reaches the agent as an httpx error string containing the internal SearXNG URL, with no fix hint. | `server.py:170-323` |
| D6 | **Input bounds.** `max_results=-1` slices off the last result instead of erroring (`results[:-1]`); `pages=0` fetches nothing and returns a "no engine returned any result" DEGRADED warning that blames the engines. | `server.py:201,313`, `clients/searxng.py:288` |
| D7 | **`pages_fetched` reports the requested page count**, not the pages actually fetched after the early `break`. | `clients/searxng.py:305,320` |
| D8 | **read_url decodes binary bodies as text.** A PDF or image returns up to 5 MB of mojibake as "markdown" (`extraction: raw`), burning context with no signal that it failed. | `clients/reader.py:116-126` |
| D9 | Module docstring says read_url falls back to raw HTML-to-markdown; the code raises `no extractable main content`. | `clients/reader.py:5` vs `:142` |

### Risks and design issues (not yet bugs)

- **R1 Dedup is exact-string URL match.** `http://` vs `https://`, `www.`, trailing slash, `#fragment`, `utm_*` / `fbclid` variants survive as separate results (`clients/searxng.py:181-200`). To be measured.
- **R2 Ranking sorts by `engine_count` before relevance**, and sums SearXNG scores across pages. A 2-engine hit at position 40 outranks a 1-engine hit at position 1. Descriptions call this "trustworthy coverage" (`server.py:292-293`, README "built-in proxy for trustworthiness"): engine agreement measures index overlap, not truth.
- **R3 Token cost.** Every response is `json.dumps(indent=2)` (pete-mcp-core `format_response`); the six-line provenance paragraph is repeated verbatim in six tool descriptions; `search_deep` defaults to 50 results.
- **R4 SSRF edge**: IPv6 transition prefixes that embed an IPv4 address (NAT64 `64:ff9b::/96`, 6to4 `2002::/16`, Teredo) are not unwrapped before the range check. Exploitable only where a NAT64/6to4 relay exists. To be confirmed by test on Python 3.13.
- **R5 search_person** presents search matches as "vetting": no disambiguation signal, `site:twitter.com` only (no x.com), name-only `social` angle ignores location, and consensus language implies verification. Names are also logged at INFO (`clients/searxng.py:399`).
- **R6 Queries logged at INFO** (`clients/searxng.py:244`), which puts personal research into container logs.
- **R7 No type check in CI**; lint ruleset is the minimal `E4,E7,E9,F`.
- **R8 Adoption friction**: not on PyPI (blocked because `pete-mcp-core` is a tarball pin), Docker quick start requires the user to stand up and configure SearXNG separately (json format off by default is the #1 failure).
- Out-of-repo: `.claude/skills/ridemap/SKILL.md:39` still says the server has "no auth", which has been false since 0.5.0 went live (probe returned 401).

### Verified sound (keep)

Redirect-by-hop SSRF validation with DNS pinning and SNI (`reader.py:49-98`), IPv6 pinning (probed: brackets correctly), CGNAT/Tailscale block via `is_global`, fail-closed bearer auth on HTTP, hash-pinned lockfile with drift check, Trivy image scan, non-root container, tokenless healthcheck that avoids session leaks, the `provenance` block.

## Phase 2: competitor comparison

Seven servers read at source level (none run): ihor-sokoliuk (TS, 1,377 stars, the default choice), TadMSTR (TS, ML rerank), bumbaRasch (TS, 13 tools, closest match), searxNcrawl (Py, 192 stars), tisDDM, SecretiveShell, jae-jae.

| | **ours** | ihor | bumbaRasch | TadMSTR | searxNcrawl |
|---|---|---|---|---|---|
| HTTP auth | **required, refuses to start** | opt-in | required off-localhost | opt-in, warns | none |
| SSRF: per-hop redirects + DNS pin | yes | yes | yes | partial (TOCTOU on some tiers) | **none** |
| Embedded-IPv4 (NAT64/6to4) block | **no** | yes | yes (BlockList) | yes | no |
| Body cap streamed / binary rejected | **no / no** | yes / yes | yes / yes | yes / yes | unverified |
| Per-result engine provenance + degraded flag | **yes, every call** | degraded error only on empty | `doctor` tool | no | consensus ordering |
| Cross-page dedup | exact URL | canonical URL | no | across LLM variants | normalized URL |
| Ranking | engine_count, then score | SearXNG score | heuristic | FlashRank + recency | consensus first |
| Domain filter | **no** | no | domain boost | `site`, block/boost | no |
| Deep research | multi-page | pages 1-5 concurrent | iterative loop, optional LLM | LLM query expansion | crawls results |
| PDF | no | yes | yes | yes | yes (Chromium) |
| Install | Docker, git uvx | npm, signed Docker Hub | npm, Docker | npm, Docker | PyPI, .mcpb |
| Official MCP registry | **not listed** | listed | listed | no | listed |

What is genuinely distinctive here: enforced auth plus a pinned, per-hop SSRF guard (only bumbaRasch matches both), the per-call `provenance` block (nobody else reports corroboration on a successful result), and the person and tech presets (none found elsewhere). What is table stakes we lack: embedded-IPv4 blocking, a streamed size cap, binary rejection, canonical-URL dedup, registry presence. So R4 and D8 move to P1 and the README's "no other SearXNG MCP ships" line survives only for search_person.

**Flagship verdict on `search_deep`**: it can be the reason to choose this server, but not by adding LLM calls (TadMSTR and bumbaRasch already do that, and it adds a key and a cost). The defensible version is a **no-LLM, provenance-honest sweep**: multiple pages and optional agent-supplied phrasings, canonical-URL dedup, domain filters, compact output, and a per-result record of which engines, pages and phrasings found it. Each part is measured in the replay benchmark against v0.5.0 before it is claimed.

## Phase 3/6: prioritized changes

| Improvement | Current limitation | Benefit | Cx | Risk | Pri |
|---|---|---|---|---|---|
| Stream read_url with byte cap (D1) | full buffer, OOM | bounded memory | S | L | P0 |
| Total deadline across hops (D2) | slow-drip hangs | bounded latency | S | L | P0 |
| Async DNS in guard (D3) | stalls all sessions | concurrency | S | L | P0 |
| Locked rate limiter + in-flight coalescing (D4) | spacing breaks under load | protects engines from IP blocks | S | L | P0 |
| Actionable search errors via `ToolError` (D5) | raw httpx text, leaks URL | agent can self-correct | S | L | P0 |
| Clamp inputs, true `pages_fetched` (D6, D7) | wrong output | correctness | S | L | P0 |
| Reject binary content types, add `code` to every read_url error (D8) | mojibake in context | tokens, clear failure | S | L | P1 |
| URL normalization for dedup (R1) | duplicates survive | fewer dupes (measured) | S | L | P1 |
| Ranking: pick by benchmark (R2) | consensus over relevance | relevance (measured, or no change) | M | M | P1 |
| Compact JSON, drop nulls (R3) | indent=2 everywhere | tokens (measured) | S | L | P1 |
| Rewrite tool descriptions (R3, R2) | repeated paragraph, overclaims | selection accuracy, manifest tokens | S | L | P1 |
| search_person reframe (R5) | implies verification | fewer unsupported conclusions | S | L | P1 |
| `include_domains` / `exclude_domains` on search + search_deep | none | targeted research | S | L | P1 |
| SSRF: unwrap NAT64/6to4/Teredo (R4) | edge bypass; competitors block it | parity, defense in depth | S | L | P1 |
| `server.json` for the official MCP registry (GHCR OCI package), not submitted | not listed; 3 competitors are | discoverability | S | L | P2 |
| Query logging to DEBUG (R6) | personal data in logs | privacy | S | L | P2 |
| Startup probe of SearXNG `/config` (warn, never fatal) | json-disabled found at first call | faster setup diagnosis | S | L | P2 |
| `examples/with-searxng/` compose (SearXNG + server, json pre-enabled) | two-system setup | one-command start | S | L | P2 |
| mypy on `src/` in CI (R7) | no type gate | standing gate | S | L | P2 |
| `search_deep` `variants`: agent-supplied rephrasings fused with RRF | single phrasing | recall (measured) | M | M | P2, ship only if benchmark shows gain |
| Publish pete-mcp-core to PyPI, then mcp-searxng (`uvx mcp-searxng`) | git-only install | adoption | M | L | P3, other repo, needs Pete |
| PDF extraction in read_url | unsupported | coverage | M | M (new dep) | P3 |
| Collapse search_news/tech/images/videos into `search` | 9 tools | smaller manifest | M | H (breaks contracts) | P3, do not do |

Contract policy: all existing tool names, parameters, defaults and response keys stay. New parameters are optional. New response keys are additive (`code`, `total_length`, `caveat`, `pages_requested`). Search failures move from an unhandled exception to a `ToolError` with an actionable message; both are `isError: true` to the client, so callers see the same class of result.

## Implementation steps

1. **Setup**: clone fresh into the scratchpad, branch `review/2026-11-hardening`, `pip install -e .[dev]` in a scratchpad venv, run `ruff check .` and `pytest tests/` to record the baseline.
2. **Benchmark harness first** (`bench/`, before changing behavior, so the baseline is the unmodified code):
   - `bench/queries.yaml`: about 30 queries across research, technical, news and entity lookups, each with a small gold set of expected domains or URLs, **written before any ranking change** and committed.
   - `bench/capture.py`: hits live SearXNG (`http://192.168.86.20:8888`) for pages 1 to 5 of each query and saves the raw JSON as fixtures with SearXNG version, engine lineup and unresponsive engines.
   - `bench/replay.py`: runs v0.5.0 (`git worktree` at the tag) and the branch against the **same** fixtures via respx. Reports duplicate rate (post-normalization duplicates remaining), Success@5, MRR, nDCG@10 against the gold set, response bytes and an estimated token count (chars/4, labelled as an estimate), manifest size.
   - `bench/live.py`: latency median and p95 per tool (cache off, N=20 each), concurrency 1/4/8 with error rate, peak RSS from `/proc/self/status`; plus a read_url run over 20 real URLs.
   - Live runs are noisy because engines get suspended (today: google and startpage suspended, 4 engines answering), so every live report records the engine lineup per run.
3. **P0 fixes** in `clients/reader.py`, `clients/ssrf.py`, `clients/searxng.py`, `server.py`, each with a test that fails on the old code: streamed oversize body aborts, slow-drip hits the deadline, guard resolves via the loop, 5 concurrent searches are spaced and 5 identical ones make one upstream call, 403/429/connect errors map to actionable `ToolError`s, clamped inputs, true `pages_fetched`.
4. **P1**: URL normalization (reuse in `deduplicate`; keep the first-seen original URL), content-type gate, error codes, compact output (a local `_format` using `json.dumps(separators=(",", ":"))` and null-dropping, so pete-mcp-core is untouched), tool description rewrite, search_person reframe (adds `caveat`, x.com, location on the social angle, DEBUG logging), domain filters (query `site:` plus a post-filter on normalized host).
5. **Ranking decision**: compare current order, SearXNG native order, and reciprocal rank fusion across pages with engine_count as a tiebreak on the replay fixtures. Ship whichever wins on nDCG@10; if none wins clearly, keep the current order and say so.
6. **P2** items from the table, `variants` only if replay shows a recall gain.
7. **Docs**: README rewrite (positioning line "Self-hosted web research for AI agents. No paid search API required.", Claude Code and generic client setup, compose-with-SearXNG quick start, config reference, security model, tool reference with examples, troubleshooting, architecture sketch), CHANGELOG `[Unreleased]`, suggested GitHub description and topics (text only, not applied), a registry list (official MCP registry, Smithery, Glama, mcp.so, PulseMCP, awesome-mcp-servers) with no submissions, a recorded demo script in `bench/demo.md` showing provenance and deep search against a plain search call.
8. **Report**: `docs/review-2026-11.md` in the repo with the seven deliverables (executive assessment, prior art, findings, implemented changes with raw test and benchmark output, roadmap, adoption strategy, README intro), plus this plan moved to `docs/plans/`. Scoped commits on the branch.
9. **Ship (approved by Pete: PR, review, merge)**: push the branch (bare `git push`), open one PR with `gh pr create`. Launch one separate read-only Opus reviewer with the PR number, told to read every line and try its own mutations against the new tests; it must not merge. Fix its notes (a second commit with real logic gets its own short review). Merge with a bare `gh pr merge <n> -R pete-builds/mcp-searxng --squash --match-head-commit <reviewed sha>` once checks are green. Then bump the superproject's submodule pointer to the merged commit with a scoped commit.

## Verification

- V1: `ruff check .`, `pytest tests/ -v`, the lockfile drift check from `ci.yml`, and `docker build` with the Python-version assertion, raw output pasted.
- V2: every new test is shown failing against the unmodified code first (control), then passing. Every benchmark metric is run on v0.5.0 and the branch from the same fixtures.
- Live: `tests/smoke.py` against nix1 SearXNG, and the branch server run locally on stdio and on HTTP with a token, exercised by a real MCP client (`fastmcp` client script) through a multi-step research flow (search_deep, then read_url on the top three, then search_news).
- Not verified, stated in the report: other MCP clients than FastMCP's and Claude Code, Docker Desktop on Mac, and any head-to-head numbers against competitor servers (comparison there is from source only).

## Stops for Pete

Approved: push, PR, independent review, merge. Still stops for Pete: any release or tag, the nix1 deploy, applying the GitHub description and topics (proposed as text only), and any registry submission. search_person: keep it, reframe it as candidate matches (Pete's choice), and move it out of the README headline. The ridemap skill's stale "no auth" line is reported, not edited.
