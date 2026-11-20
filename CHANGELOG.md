# Changelog

Notable changes to mcp-searxng. Versions follow [Semantic Versioning](https://semver.org/spec/v2.0.0.html).
Releases before 0.5.0 have no entry here: see the git tags.

## [Unreleased]

Measured against 0.5.0 on recorded SearXNG answers; method and limits in
`bench/README.md`.

### Fixed

- **read_url held whole responses in memory.** The 5 MB cap was checked after
  the full download. The body is now streamed and abandoned at the cap, and a
  declared `Content-Length` over it is refused unread.
- **read_url had no overall deadline.** A server sending a byte at a time kept
  a fetch open indefinitely. Every read now has a 45 s wall-clock budget across
  all redirect hops and the body.
- **read_url returned binary files as text.** A PDF came back as 2 million
  characters of mojibake labelled markdown. PDFs, images and other binary types
  are refused before download with `code: UNSUPPORTED_CONTENT_TYPE`. JSON, XML,
  CSV and plain text still come back raw.
- **The SSRF guard blocked the event loop** on every DNS lookup, stalling all
  sessions behind one slow resolver. Lookups run in a worker thread.
- **The 1 s request spacing did not hold under concurrency.** Concurrent
  callers all fired at once. It is now enforced across sessions, and identical
  in-flight queries share one upstream request.
- **SearXNG errors reached agents as raw httpx text** containing the internal
  SearXNG URL. They are now tool errors that name the fix, with a code:
  `SEARXNG_FORBIDDEN` (JSON API off), `SEARXNG_RATE_LIMITED`,
  `SEARXNG_UNREACHABLE`, `SEARXNG_TIMEOUT`, `SEARXNG_NOT_JSON`,
  `SEARXNG_HTTP_ERROR`.
- `max_results=-1` dropped the last result; `pages=0` returned a warning that
  blamed the engines. Both are clamped. `pages_fetched` now reports pages
  actually fetched (it echoed the request).
- `search_person` returned zero results, not an error, when SearXNG was down
  for every angle.

### Changed

- **search_deep ordering.** SearXNG's own order for the top 10, then the best
  score each result earned on any page. The old sort by engine count first
  lowered relevance: nDCG@10 0.427 to 0.492 on a held-out query set, Recall@50
  unchanged. `engine_count` is still on every result.
- **search_deep merges the same page under different URLs** (scheme, `www.`,
  trailing slash, fragment, `utm_*` and click IDs). The URL kept is one an
  engine returned.
- **Responses are compact JSON** with non-ASCII left as text: about 19% fewer
  characters for the same results.
- **Tool descriptions** say which tool to use when, state parameter ranges, and
  no longer call engine agreement "trustworthy".
- **search_person** keeps its contract and adds `caveat`, a per-result `match`
  record (does the result's own text contain the name and the location),
  `name_matches` and `failed_angles`. The social angle includes x.com and the
  location. Results whose text names the person come first.
- read_url errors all carry a `code`; HTTP errors add `status`; truncation with
  `max_chars` adds `total_length`.
- Queries and person names are logged at DEBUG, not INFO.

### Added

- `include_domains` and `exclude_domains` on `search` and `search_deep`.
- `SEARXNG_MIN_INTERVAL` (default 1.0 s; 0 disables request spacing).
- A startup check that warns, with the fix, when SearXNG's JSON API is off, its
  limiter is on, or the URL is wrong. Never fatal; no engine traffic.
- `examples/with-searxng`: SearXNG and this server in one compose file, run by
  CI on every pull request.
- `server.json` and an image label for the official MCP registry (not yet
  submitted).
- `bench/`: recordings, a gold-set relevance benchmark with a holdout set, a
  live benchmark, and a demo transcript.
- mypy on `src/` in CI.

## [0.5.0] (2026-11-20)

### Breaking

- **The HTTP server now requires a bearer token, and refuses to start without one.**
  Until this release the server ran with no token and answered anyone who could
  reach port 3702. From 0.5.0:

  - With no `MCP_AUTH_TOKEN`, the HTTP transport does not start. The process
    exits with status 1 and logs
    `FATAL: Bearer token required for client 'searxng' but not provided`.
    A container with `restart: unless-stopped` restarts in a loop until the
    token is set.
  - With a token set, a request with no `Authorization` header, or with a wrong
    token, gets `401`. Only `Authorization: Bearer <token>` is served.
  - There is no setting that opens HTTP again. `MCP_AUTH_REQUIRED=false` does
    not: the server still refuses to start.

  **Before you deploy 0.5.0**, generate a token and put it in `.env` on the host:

  ```bash
  python3 -c "import secrets; print(secrets.token_urlsafe(32))"
  ```

  ```bash
  MCP_AUTH_TOKEN=<the token>
  ```

  Then `chmod 600 .env`. The token belongs in `.env` only, never in a compose
  file or in git. Every client must then send the same token. In a Claude Code
  server entry that is:

  ```json
  "headers": { "Authorization": "Bearer <the token>" }
  ```

  A client that is not updated gets `401` on every call.

### Unchanged

- **stdio needs no token.** `MCP_TRANSPORT=stdio` runs the server as a local
  pipe with no network surface, exactly as before.
- **The container healthcheck needs no token.** It probes a path outside the
  MCP endpoint and counts the `404` it gets back as healthy, so turning auth on
  does not make the container unhealthy.

### Changed

- pete-mcp-core moves from `60cc81e` to `18f2261`. That is where the fail-closed
  default comes from. It also carries session reaper fixes: `MCP_SESSION_IDLE_TIMEOUT=off`
  now really means no idle timeout, and the timeout no longer depends on which
  FastMCP and MCP SDK versions happen to be installed.
- `serverInfo.version` in the `initialize` response now reports this server's
  release (`0.5.0`). It used to report FastMCP's own version (`4.2.2`), which
  told a client nothing about which release of mcp-searxng it was talking to.
- The release workflow refuses to build when the tag and the `pyproject.toml`
  version disagree, so a published image cannot report a version it is not.
- `MCP_TRANSPORT` unset now means HTTP in the settings object too, matching what
  the server has always started. Nothing changes for a deployment that did not
  set it.

### Tests

- `tests/test_auth_required.py` holds the behavior above against the real server
  module, its real ASGI app, and a real subprocess: both refusals to start, the
  three `401` and `200` cases, the stdio exemption, and the tokenless healthcheck.
- `tests/test_server_version.py` boots the real server and checks `serverInfo.version`
  against `pyproject.toml`.
