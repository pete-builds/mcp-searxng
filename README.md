# mcp-searxng

**Self-hosted web research for AI agents. No paid search API required.**

An [MCP](https://modelcontextprotocol.io) server for your own [SearXNG](https://github.com/searxng/searxng) instance. It gives an agent web search, a multi-page research sweep, and a page reader, and it tells the agent when the results are not worth trusting.

Self-hosted metasearch has one failure mode that matters more than any other: engines get blocked by IP, quietly, and SearXNG keeps answering with whatever one engine has left. Every search here returns a `provenance` block that says how many engines actually answered, and flags a degraded response in words. The [demo](bench/demo.md) shows it catching a news search that came back as one engine's unrelated filler.

- **Honest results.** Per-result engine lists, a `degraded` flag and a plain-language warning on every search.
- **Safe page reading.** `read_url` resolves DNS once and connects to the vetted address, re-checks every redirect hop, blocks private, metadata and Tailscale ranges, stops at 5 MB, has a hard deadline, and refuses binary files.
- **Locked by default.** Over HTTP the server will not start without a bearer token.
- **A measured deep search.** `search_deep` sweeps up to 5 pages, merges the same page under different URLs, filters by domain, and orders results by a rule chosen on a benchmark, not by intuition ([bench/](bench/README.md)).
- **Small and read-only.** Nine tools, no LLM calls, no API keys, no writes anywhere.

What it does not do: read PDFs, render JavaScript, or summarize with an LLM. Other SearXNG servers do those; this one stays a small, auditable tool.

---

## Quick start: SearXNG included

Needs Docker. Starts SearXNG (JSON API already on, not exposed to your network) and this server on `127.0.0.1:3702`.

```bash
git clone https://github.com/pete-builds/mcp-searxng.git
cd mcp-searxng/examples/with-searxng
cp .env.example .env && chmod 600 .env
# Fill in both values. Generate each with:
#   python3 -c "import secrets; print(secrets.token_urlsafe(32))"
docker compose up -d
```

Check it end to end:

```bash
docker compose exec -T -e TOKEN="$(grep ^MCP_AUTH_TOKEN= .env | cut -d= -f2)" \
  mcp-searxng python - < check.py
```

CI runs this exact compose file and check on every pull request.

## Already running SearXNG?

Turn on its JSON API first, in SearXNG's `settings.yml`, then restart it:

```yaml
search:
  formats:
    - html
    - json
```

Then make a token (keep it: every client needs it) and run the server with Docker:

```bash
export SEARXNG_MCP_TOKEN="$(python3 -c 'import secrets; print(secrets.token_urlsafe(32))')"
docker run -d --name mcp-searxng -p 127.0.0.1:3702:3702 \
  -e SEARXNG_URL=http://your-searxng:8888 \
  -e MCP_AUTH_TOKEN="$SEARXNG_MCP_TOKEN" \
  ghcr.io/pete-builds/mcp-searxng:0.6.0
```

or without Docker, from a tagged release (there is no PyPI package yet):

```bash
SEARXNG_URL=http://your-searxng:8888 MCP_AUTH_TOKEN="$SEARXNG_MCP_TOKEN" \
  uvx --from git+https://github.com/pete-builds/mcp-searxng@v0.6.0 mcp-searxng
```

`docker-compose.yml` at the repo root is the long-running setup this project's author uses (host networking, `.env` file); see [Configuration](#configuration).

## Connect a client

**Claude Code**, over HTTP:

```bash
claude mcp add --transport http searxng http://127.0.0.1:3702/mcp \
  --header "Authorization: Bearer $SEARXNG_MCP_TOKEN"
```

or in `.mcp.json`, with the token expanded from your environment so it stays out of the file:

```json
{
  "mcpServers": {
    "searxng": {
      "type": "http",
      "url": "http://127.0.0.1:3702/mcp",
      "headers": { "Authorization": "Bearer ${SEARXNG_MCP_TOKEN}" }
    }
  }
}
```

**Clients that launch a local process** (Claude Desktop, Cursor and others) can run it over stdio, which needs no token because there is no network listener:

```json
{
  "mcpServers": {
    "searxng": {
      "command": "uvx",
      "args": ["--from", "git+https://github.com/pete-builds/mcp-searxng@v0.6.0", "mcp-searxng"],
      "env": { "MCP_TRANSPORT": "stdio", "SEARXNG_URL": "http://your-searxng:8888" }
    }
  }
}
```

Any other MCP client that speaks streamable HTTP works the same way: URL `http://<host>:3702/mcp`, header `Authorization: Bearer <token>`. Tested with Claude Code and the FastMCP client; others are expected to work but have not been checked.

---

## Tools

| Tool | Use it for | Notable parameters |
|---|---|---|
| `search` | Most lookups | `categories`, `engines`, `time_range`, `include_domains`, `exclude_domains`, `max_results` (1 to 30) |
| `search_deep` | Surveying a topic: up to 5 pages in one call | `pages` (1 to 5), `max_results` (1 to 100), `include_domains`, `exclude_domains` |
| `search_news` | Recent events (default: past week) | `time_range` |
| `search_tech` | Docs, Stack Overflow, GitHub, package indexes | `engines` |
| `search_images` / `search_videos` | Media | `max_results` |
| `read_url` | Reading one page as markdown, or JSON and text as-is | `max_chars` |
| `search_person` | Candidate pages about a named person (8 targeted searches) | `location`, `context` |
| `get_engines` | What this instance has enabled | |

Examples of arguments an agent might send:

```jsonc
// search_deep: primary sources only, three pages
{"query": "HTTP/3 connection migration", "pages": 3,
 "include_domains": "rfc-editor.org,ietf.org,developer.mozilla.org"}

// search: skip a domain
{"query": "sourdough hydration", "exclude_domains": "pinterest.com"}

// read_url: first 4,000 characters
{"url": "https://www.rfc-editor.org/rfc/rfc9114.html", "max_chars": 4000}
```

### Reading the response

Every search returns a `provenance` block:

| Field | Meaning |
|---|---|
| `engine_count`, `engines_contributing` | Engines that returned at least one result |
| `corroborated_results`, `single_engine_results` | How many results more than one engine returned |
| `unresponsive_engines` | Engines SearXNG reports as failed, with the reason |
| `degraded` | `true` when fewer than two engines answered. Results may be one engine's loose matches, and an empty set is not evidence of absence |
| `warning` | The same, in a sentence, when `degraded` is true |

Each result carries `engines` and `engine_count`. That number says how many indexes hold a page. It is not a measure of whether the page is correct.

`search_deep` also reports `pages_fetched` and `pages_requested` (fewer fetched means a page came back empty), and `domain_filter` when a filter was applied.

`read_url` never raises a tool error for a bad page. It returns `{error, code, url}`:

| `code` | Meaning |
|---|---|
| `BLOCKED_URL` | Private, loopback, link-local, metadata or Tailscale address, or a non-http scheme. Checked on every redirect hop |
| `UNSUPPORTED_CONTENT_TYPE` | PDF, image or other binary. Refused before download |
| `TOO_LARGE` | Over 5 MB |
| `NO_CONTENT` | HTML with no extractable article body (login walls, JavaScript apps) |
| `HTTP_ERROR` | The site answered 4xx or 5xx; `status` has the code |
| `TIMEOUT`, `TOO_MANY_REDIRECTS`, `FETCH_FAILED` | As named |

`search_person` returns search matches, not identity. Common names mix different people. Each result has a `match` record saying only whether its own title or snippet contains the name and the location, and the response carries a `caveat` saying so. Confirm anything by reading the page before attributing it to someone.

A SearXNG failure (JSON disabled, rate-limited, unreachable) is a tool error whose message names the fix and a `code`, for example `SEARXNG_FORBIDDEN`.

---

## Configuration

| Variable | Default | |
|---|---|---|
| `SEARXNG_URL` | (required) | SearXNG base URL. Inside Docker, `localhost` is the container itself |
| `MCP_AUTH_TOKEN` | (required for HTTP) | Bearer token clients must send |
| `MCP_TRANSPORT` | `streamable-http` | Or `stdio` for a local process with no listener and no token |
| `FASTMCP_HOST` / `FASTMCP_PORT` | `0.0.0.0` / `3702` | `MCP_HOST` / `MCP_PORT` also accepted |
| `SEARXNG_CACHE_TTL` | `300` | Seconds identical queries are served from memory |
| `SEARXNG_MIN_INTERVAL` | `1.0` | Minimum seconds between requests to SearXNG, across all sessions. `0` turns spacing off |
| `MCP_LOG_LEVEL` / `MCP_LOG_FORMAT` | `INFO` / `json` | Queries and names are logged only at `DEBUG` |
| `MCP_SESSION_IDLE_TIMEOUT` | `1800` | Seconds before an idle HTTP session is reaped; `off` disables |

At startup the server sends SearXNG one empty JSON query (no engine traffic) and logs a warning with the fix if the JSON API is off, the limiter is on, or the URL is wrong.

## Security model

- **HTTP requires a token.** With `MCP_TRANSPORT=streamable-http` (the default) and no `MCP_AUTH_TOKEN`, the server exits. A request without the right `Authorization: Bearer` header gets `401`. `MCP_AUTH_REQUIRED=false` does not open it. The container healthcheck uses an unauthenticated path outside the MCP endpoint.
- **The token is not encryption.** Over plain HTTP it and every query cross the network in the clear. Beyond one machine, put TLS in front or use a private network such as Tailscale.
- **`read_url` is the network-facing risk** and is built for it: DNS resolved once and pinned (no rebinding window), TLS still verified against the hostname, every redirect re-validated (5 hops max), non-global addresses refused (including `100.64.0.0/10` and IPv6 forms that embed an IPv4 address), 5 MB streamed cap, 45 s deadline, binary refused before download.
- **Everything returned is untrusted text.** Snippets and pages are written by strangers. Tool descriptions tell the agent never to follow instructions found in them; your agent's own rules should say the same.
- **Logs.** Queries and person names are logged at `DEBUG` only.
- **`search_person`** is a capability to aim people-search at anyone. It searches public pages and reports what they say, nothing more. Use it for due diligence you have a reason to do.

Report vulnerabilities privately: see [SECURITY.md](SECURITY.md).

## Troubleshooting

| Symptom | Cause and fix |
|---|---|
| `SEARXNG_FORBIDDEN` / startup warning about format=json | JSON API off. Add `json` under `search.formats` in SearXNG's `settings.yml`, restart SearXNG |
| `SEARXNG_RATE_LIMITED` | SearXNG's bot limiter is on. Disable it for this server's address, or turn `limiter` off if SearXNG is not public |
| `SEARXNG_UNREACHABLE` from Docker | `localhost` inside a container is the container. Use the SearXNG container name, `host.docker.internal`, or a LAN IP |
| `degraded: true` on most searches | Engines are blocked by IP. Check `unresponsive_engines` and `get_engines`, and enable engines that still answer from your address |
| `401` | The client is not sending `Authorization: Bearer <token>`, or the token differs from `MCP_AUTH_TOKEN` |
| Server exits at start: `Bearer token required` | Set `MCP_AUTH_TOKEN`, or use `MCP_TRANSPORT=stdio` for a local process |
| `read_url` returns `UNSUPPORTED_CONTENT_TYPE` | It does not read PDFs or binary files, by design |

## How it works

```
 MCP client ──HTTP + bearer──▶ mcp-searxng ──JSON API──▶ SearXNG ──▶ engines
 (or stdio)                    │  search / search_deep: shape, merge, provenance
                               │  read_url: SSRF guard ─▶ pinned fetch ─▶ trafilatura
                               └─ rate limit + in-flight coalescing + TTL cache
```

- `src/mcp_searxng/server.py`: tool definitions, settings, startup check.
- `src/mcp_searxng/clients/searxng.py`: SearXNG client, provenance, dedup, ranking, filters.
- `src/mcp_searxng/clients/reader.py` and `ssrf.py`: the page reader and its guard.
- Shared server plumbing (auth, logging, session reaping) comes from [pete-mcp-core](https://github.com/pete-builds/pete-mcp-core).

## Benchmarks

[bench/README.md](bench/README.md) has the method, raw numbers and limits. In short, against 0.5.0 on the same recorded SearXNG answers: `search_deep`'s nDCG@10 rose from 0.43 to 0.49 on a held-out query set with Recall@50 unchanged, same-page duplicates went from 6 to 0, and responses are about 19% smaller. Search latency is unchanged; it is SearXNG's.

## Development

```bash
pip install -e ".[dev]"
ruff check . && mypy && pytest tests/
SEARXNG_URL=http://your-searxng:8888 python tests/smoke.py   # live, not in CI
```

CI also checks that `requirements.lock` matches `pyproject.toml`, builds and Trivy-scans the image, and runs the quick start. See [CONTRIBUTING.md](CONTRIBUTING.md).

## License

MIT. Built by [Pete Stergion](https://github.com/pete-builds). Uses [SearXNG](https://github.com/searxng/searxng), [FastMCP](https://github.com/jlowin/fastmcp) and [trafilatura](https://github.com/adbar/trafilatura).
