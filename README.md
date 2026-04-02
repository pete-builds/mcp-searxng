# mcp-searxng

An MCP server for [Claude Code](https://claude.com/claude-code) that wraps a self-hosted [SearXNG](https://github.com/searxng/searxng) metasearch instance. Gives Claude tools for web search, news search, deep multi-page search, technical search, and people lookup.

Built with [FastMCP](https://github.com/jlowin/fastmcp). No API keys required — SearXNG aggregates results from Bing, DuckDuckGo, Brave, Reddit, and more for free.

Designed as the search backend for [claude-research-agent](https://github.com/pete-builds/claude-research-agent).

---

## Tools

| Tool | What it does |
|------|-------------|
| `search` | General web search with category, engine, language, and time filters |
| `search_news` | News search, defaults to last week |
| `search_tech` | IT-focused search: Stack Overflow, GitHub, documentation, wikis |
| `search_deep` | Multi-page fetch with URL deduplication and multi-engine consensus ranking |
| `search_person` | Fans out 8 targeted sub-searches in one call (identity, LinkedIn, business, legal, news, social, property, Reddit) |
| `get_engines` | Lists all available engines and categories on your SearXNG instance |

### search_deep

The most powerful tool. It fetches multiple pages of results, deduplicates by URL, and scores each result by how many engines returned it. Higher `engine_count` means more trustworthy. Use this for research queries where you need broad, reliable coverage.

### search_person

Designed for due diligence and vetting. A single call fans out into 8 targeted searches and returns categorized, deduplicated results. Much more efficient than running 8+ separate queries.

---

## Requirements

You need a running SearXNG instance. SearXNG is a free, self-hosted metasearch engine. The quickest way to get one running:

```bash
docker run -d -p 8888:8080 \
  -e SEARXNG_BASE_URL=http://localhost:8888 \
  --name searxng \
  searxng/searxng
```

Or use the official [SearXNG Docker documentation](https://docs.searxng.org/admin/installation-docker.html) for a full setup with a config file.

The MCP server connects to SearXNG's JSON API and requires the `json` output format to be enabled. Add this to your SearXNG `settings.yml`:

```yaml
search:
  formats:
    - html
    - json
```

---

## Setup

### 1. Clone and configure

```bash
git clone https://github.com/pete-builds/mcp-searxng.git
cd mcp-searxng
cp .env.example .env
```

Edit `.env` and set your SearXNG URL:

```
SEARXNG_URL=http://localhost:8888
```

### 2. Start the server

```bash
docker compose up -d
```

Default port: **3702** (SSE transport).

### 3. Connect to Claude Code

Add to your Claude Code `settings.json`:

```json
{
  "mcpServers": {
    "searxng": {
      "type": "sse",
      "url": "http://localhost:3702/sse"
    }
  }
}
```

Restart Claude Code. The tools show up as `mcp__searxng__*`.

---

## Configuration

```bash
# Required
SEARXNG_URL=http://your-searxng-host:8888   # URL of your SearXNG instance

# Optional (defaults shown)
MCP_HOST=0.0.0.0
MCP_PORT=3702
```

---

## Notes

- `docker-compose.yml` uses `network_mode: host`. The MCP server binds to `0.0.0.0:3702`. If running on a server, restrict access with a firewall rule.
- SearXNG must have the `json` format enabled (see setup above). The server will error if it can't get JSON responses.
- `search_deep` makes multiple requests to SearXNG (one per page). Set `pages: 3-5` for thorough research, `pages: 1-2` for quick lookups.

---

## Credits

Built by [Pete Stergion](https://github.com/pete-builds) for use with [Claude Code](https://claude.com/claude-code).

Related projects:
- [claude-research-agent](https://github.com/pete-builds/claude-research-agent): The research skill that uses this server
- [mcp-threatintel](https://github.com/pete-builds/mcp-threatintel): Threat intelligence MCP server (pairs well for security research)
- [SearXNG](https://github.com/searxng/searxng): The metasearch engine this wraps
