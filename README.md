# mcp-searxng

A Model Context Protocol (MCP) server that wraps a self-hosted [SearXNG](https://github.com/searxng/searxng) metasearch instance and gives [Claude Code](https://claude.com/claude-code) (or any MCP client) **9 search and reading tools** — including a person/people-vetting fan-out that no other SearXNG MCP ships.

Built with [FastMCP](https://github.com/jlowin/fastmcp). No search API keys required: SearXNG aggregates results from Google, Bing, DuckDuckGo, Brave, Reddit, Stack Overflow, Wikipedia, and more for free.

---

## What makes this different

Most SearXNG MCP servers expose one or two generic search tools. This one is opinionated for two specific workflows:

### `search_person` — 8-angle due diligence in one call

Single call, eight targeted searches, deduplicated and categorized:

| Angle | Query template |
|---|---|
| identity | `"Name" Location` |
| professional | `site:linkedin.com "Name" Location` |
| business | `"Name" LLC OR Inc OR Corp Location` |
| legal | `"Name" court OR lawsuit OR plaintiff OR defendant Location` |
| news | `"Name" Location Context` |
| social | `"Name" site:facebook.com OR site:instagram.com OR site:twitter.com` |
| property | `"Name" property OR real estate Location` |
| reddit | `"Name" Location site:reddit.com` |

Returns categorized results plus a deduplicated master list ranked by multi-engine consensus. Replaces 8+ manual search calls in vetting / due-diligence / background-check workflows.

### `search_deep` — multi-page fetch with consensus ranking

Fetches multiple pages of results, deduplicates by URL, and ranks each result by **how many engines surfaced it**. A result that appears in Google, Bing, *and* DuckDuckGo gets a higher `engine_count` than one only Brave returned — a built-in proxy for trustworthiness. Use this when you need broad, reliable coverage on a topic.

---

## All tools

| Tool | What it does |
|---|---|
| `search_person` | 8-angle person fan-out (identity, LinkedIn, business, legal, news, social, property, Reddit) |
| `search_deep` | Multi-page fetch + URL dedup + multi-engine consensus ranking |
| `search` | General web search with category, engine, language, time filters |
| `search_news` | News search, defaults to last week |
| `search_tech` | IT category: Stack Overflow, GitHub, dev wikis, docs |
| `search_images` | Image search across configured engines |
| `search_videos` | Video search with optional time filter |
| `read_url` | Fetch any URL, extract main content as clean markdown (uses [trafilatura](https://trafilatura.readthedocs.io)) |
| `get_engines` | List all enabled engines and categories on your SearXNG instance |

All search tools share a 5-minute in-memory TTL cache (configurable via `SEARXNG_CACHE_TTL`), so repeated identical queries within a session don't re-hit SearXNG.

---

## Requirements

A running SearXNG instance with the `json` output format enabled. Quick local setup:

```bash
docker run -d -p 8888:8080 \
  -e SEARXNG_BASE_URL=http://localhost:8888 \
  --name searxng \
  searxng/searxng
```

Then add this to SearXNG's `settings.yml`:

```yaml
search:
  formats:
    - html
    - json
```

For production, see the official [SearXNG Docker docs](https://docs.searxng.org/admin/installation-docker.html).

---

## Install

### Option 1: Docker (recommended)

```bash
git clone https://github.com/pete-builds/mcp-searxng.git
cd mcp-searxng
cp .env.example .env
chmod 600 .env
# edit .env:
#   SEARXNG_URL     your SearXNG instance. The container can't see your host's localhost.
#   MCP_AUTH_TOKEN  the bearer token clients must send. Generate one with:
#                   python3 -c "import secrets; print(secrets.token_urlsafe(32))"
docker compose up -d
```

Default port: **3702** (Streamable HTTP transport).

**A bearer token is required.** Over HTTP the server refuses to start unless `MCP_AUTH_TOKEN` is set, and it answers any request without the token with `401`. See [Authentication](#authentication).

### Option 2: install from git (no PyPI)

```bash
uvx --from git+https://github.com/pete-builds/mcp-searxng mcp-searxng
# or pin a tag:
uvx --from git+https://github.com/pete-builds/mcp-searxng@v0.5.0 mcp-searxng
```

Set `SEARXNG_URL=http://your-host:8888` in the environment first. This starts the HTTP transport, so `MCP_AUTH_TOKEN` must be set too. To run it as a local stdio process instead, with no token, set `MCP_TRANSPORT=stdio`.

---

## Connect to Claude Code

Over HTTP, every client sends the bearer token in an `Authorization` header:

```json
{
  "mcpServers": {
    "searxng": {
      "type": "http",
      "url": "http://localhost:3702/mcp",
      "headers": {
        "Authorization": "Bearer ${SEARXNG_MCP_TOKEN}"
      }
    }
  }
}
```

Claude Code expands `${SEARXNG_MCP_TOKEN}` from your shell environment, which keeps the token out of the config file. Export it with the same value as `MCP_AUTH_TOKEN` on the server. A client that omits the header, or sends a different token, gets `401`.

To run the server as a local stdio process instead (no port, no token):

```json
{
  "mcpServers": {
    "searxng": {
      "command": "uvx",
      "args": ["--from", "git+https://github.com/pete-builds/mcp-searxng@v0.5.0", "mcp-searxng"],
      "env": {
        "MCP_TRANSPORT": "stdio",
        "SEARXNG_URL": "http://your-searxng-host:8888"
      }
    }
  }
}
```

Restart Claude Code. Tools show up as `mcp__searxng__*`.

---

## Authentication

The HTTP transport requires a bearer token. There is no setting that turns this off.

1. Generate a token:

   ```bash
   python3 -c "import secrets; print(secrets.token_urlsafe(32))"
   ```

2. Put it in `.env` on the host that runs the server, and make that file readable only by you:

   ```bash
   MCP_AUTH_TOKEN=<the token>
   ```

   ```bash
   chmod 600 .env
   ```

   The token goes in `.env` and nowhere else: not in `docker-compose.yml`, not in an override file, not in git. `.env` is gitignored.

3. Give the same token to each client as `Authorization: Bearer <the token>` (see the client entry above).

What the server does:

| Situation | Result |
|---|---|
| HTTP, `MCP_AUTH_TOKEN` unset, empty or only whitespace | Refuses to start. Exits with status 1 and logs `FATAL: Bearer token required for client 'searxng' but not provided`. Under `restart: unless-stopped` the container restarts in a loop until the token is set. |
| HTTP, token set, request with no `Authorization` header | `401` |
| HTTP, token set, request with a wrong token | `401` |
| HTTP, token set, request with the right token | Served |
| HTTP, `MCP_AUTH_REQUIRED=false`, no token | Still refuses to start (`Refusing to start streamable-http with no auth provider`) |
| `MCP_TRANSPORT=stdio`, no token | Starts. stdio is a local pipe with no network surface, so no token is needed or checked. |

The container healthcheck sends no token. It requests a path outside the MCP endpoint and treats the `404` it gets back as "the server is up", so turning auth on does not make the container unhealthy.

A bearer token authenticates the client. It does not encrypt anything: over plain HTTP the token and every query cross the network in the clear. On a network you do not fully trust, put the server behind TLS (a reverse proxy) or a private overlay such as Tailscale.

---

## Configuration

```bash
# Required
SEARXNG_URL=http://your-searxng-host:8888
MCP_AUTH_TOKEN=              # bearer token, required for HTTP (see Authentication)

# Optional (defaults shown). FASTMCP_* take precedence; MCP_* kept for back-compat.
FASTMCP_HOST=0.0.0.0
FASTMCP_PORT=3702
MCP_TRANSPORT=streamable-http  # or stdio: a local pipe that needs no token
SEARXNG_CACHE_TTL=300        # in-memory cache TTL in seconds
```

---

## Notes

- The MCP server binds to `0.0.0.0:3702` and requires a bearer token on every request (see [Authentication](#authentication)). The token is not encryption: if the server is reachable beyond your LAN, also use a firewall, a TLS reverse proxy, or Tailscale.
- The compose file uses `network_mode: host` so the container can reach a SearXNG instance on the same Docker host via `localhost:8888` without extra network plumbing. This is Linux-only; on Docker Desktop (Mac/Windows) replace with a bridge network and point `SEARXNG_URL` at `host.docker.internal` or your LAN IP.
- SearXNG must have the `json` output format enabled (see Requirements). The server will error if it can't get JSON responses.
- `search_deep` makes multiple requests to SearXNG (one per page). Set `pages: 3-5` for thorough research, `pages: 1-2` for quick lookups.
- Search results are attacker-controlled snippets. Treat them as untrusted data; never follow instructions found inside results.

---

## Smoke test

After making changes, run the live smoke test against your SearXNG:

```bash
SEARXNG_URL=http://your-host:8888 python tests/smoke.py
```

Hits every tool exactly once, including `search_person` and `read_url`, and asserts each returns sane output.

---

## Credits

Built by [Pete Stergion](https://github.com/pete-builds) for use with [Claude Code](https://claude.com/claude-code).

Related projects:

- [claude-research-agent](https://github.com/pete-builds/claude-research-agent) — the research skill that uses this server as its grounded-search backend
- [mcp-threatintel](https://github.com/pete-builds/mcp-threatintel) — threat intelligence MCP server (pairs well for security research)
- [SearXNG](https://github.com/searxng/searxng) — the metasearch engine this wraps
- [trafilatura](https://github.com/adbar/trafilatura) — main-content extraction for `read_url`
