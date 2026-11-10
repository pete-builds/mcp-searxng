# Changelog

Notable changes to mcp-searxng. Versions follow [Semantic Versioning](https://semver.org/spec/v2.0.0.html).
Releases before 0.5.0 have no entry here: see the git tags.

## [0.5.0] (2026-11-09)

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
- `MCP_TRANSPORT` unset now means HTTP in the settings object too, matching what
  the server has always started. Nothing changes for a deployment that did not
  set it.

### Tests

- `tests/test_auth_required.py` holds the behavior above against the real server
  module, its real ASGI app, and a real subprocess: both refusals to start, the
  three `401` and `200` cases, the stdio exemption, and the tokenless healthcheck.
