"""HTTP needs a bearer token, and the server refuses to start without one.

Until pete-mcp-core 18f2261 this server ran on the LAN with no token and
answered every unauthenticated request with 200. Nothing was red: the suite
passed, the container was healthy, and all nine tools were open to anything
that could reach the port. These tests pin the other behavior, against the
real server module and its real ASGI app rather than against the library.

Three things are held here, each of which can fail on its own:

1. No token, HTTP transport: the process does not start. There are two
   independent refusals (the module will not load, and ``run_server`` will not
   serve an app with no auth provider), and each has its own test, so removing
   either one turns something red.
2. With a token: a request without it, or with a wrong one, is 401 before it
   reaches MCP. The right one is let through and ``initialize`` succeeds.
3. The container healthcheck stays green with auth on. Its probe carries no
   token by design. If it probed a guarded path and treated 401 as a fault,
   turning auth on would put the container in a restart loop.

stdio is the one transport that runs with no token, because it is a local pipe
with no network surface. That is asserted too, so the exemption cannot widen.

Every server here is a fresh import of ``mcp_searxng.server`` under an exact
environment. The tokens are fakes and say so.
"""

from __future__ import annotations

import asyncio
import contextlib
import importlib
import json
import os
import socket
import subprocess
import sys
import time
from collections.abc import Awaitable, Callable, Iterator
from types import ModuleType
from typing import Any

import fastmcp
import httpx
import pytest
from mcp.server.streamable_http_manager import StreamableHTTPSessionManager
from pete_mcp_core.auth import AuthRequiredError
from pete_mcp_core.healthcheck import (
    DEFAULT_HEALTH_PATH,
    TRANSPORT_PATHS,
    _resolve_healthy_codes,
)

MODULE = "mcp_searxng.server"
HEALTHCHECK_MODULE = "mcp_searxng.healthcheck"

#: Never contacted. ``initialize`` does not touch the upstream.
DUMMY_UPSTREAM = "http://searxng.invalid:8888"

TOKEN = "test-token-not-a-secret"
WRONG_TOKEN = "wrong-test-token-not-a-secret"

#: A loopback Host header, so FastMCP's host check has nothing to object to and
#: a 401 can only have come from the auth layer.
BASE_URL = "http://127.0.0.1:3702"

INITIALIZE = {
    "jsonrpc": "2.0",
    "id": 1,
    "method": "initialize",
    "params": {
        "protocolVersion": "2025-06-18",
        "capabilities": {},
        "clientInfo": {"name": "auth-required-test", "version": "0"},
    },
}
MCP_HEADERS = {
    "Content-Type": "application/json",
    "Accept": "application/json, text/event-stream",
}


def _clean_env(extra: dict[str, str]) -> dict[str, str]:
    """The calling environment minus everything that could decide the outcome.

    A developer shell that exports MCP_AUTH_TOKEN, or a repo ``.env`` that sets
    one, would otherwise make the refusal tests pass or fail for reasons that
    have nothing to do with the code. PYTHON_DOTENV_DISABLED stops the server's
    own ``load_dotenv()`` from reading a ``.env`` back in.
    """
    env = {k: v for k, v in os.environ.items() if not k.startswith(("MCP_", "FASTMCP_"))}
    env.update(
        {
            "SEARXNG_URL": DUMMY_UPSTREAM,
            "PYTHON_DOTENV_DISABLED": "1",
            "FASTMCP_CHECK_FOR_UPDATES": "off",
            "FASTMCP_SHOW_SERVER_BANNER": "false",
        }
    )
    env.update(extra)
    return env


@pytest.fixture
def load_server(monkeypatch: pytest.MonkeyPatch, tmp_path) -> Callable[..., ModuleType]:
    """Import a fresh copy of the real server module under exactly this environment."""

    def load(**extra: str) -> ModuleType:
        for name in list(os.environ):
            if name.startswith(("MCP_", "FASTMCP_")):
                monkeypatch.delenv(name)
        for name, value in _clean_env(extra).items():
            monkeypatch.setenv(name, value)
        # pydantic-settings reads ".env" relative to the working directory.
        monkeypatch.chdir(tmp_path)
        # delitem restores the suite's shared copy of the module on teardown.
        monkeypatch.delitem(sys.modules, MODULE, raising=False)
        return importlib.import_module(MODULE)

    return load


def _never_serve(server: ModuleType, monkeypatch: pytest.MonkeyPatch) -> list[dict[str, Any]]:
    """Replace ``mcp.run`` with a recorder, so a missing refusal fails the test
    instead of binding a port and serving forever."""
    calls: list[dict[str, Any]] = []
    monkeypatch.setattr(server.mcp, "run", lambda **kwargs: calls.append(kwargs))
    # main() writes the idle timeout into FastMCP's global settings and patches
    # the SDK session manager for the whole process. Put both back afterwards.
    monkeypatch.setattr(
        fastmcp.settings,
        "http_session_idle_timeout",
        fastmcp.settings.http_session_idle_timeout,
    )
    monkeypatch.setattr(
        StreamableHTTPSessionManager, "__init__", StreamableHTTPSessionManager.__init__
    )
    return calls


def _drive(server: ModuleType, exchange: Callable[[httpx.AsyncClient], Awaitable[Any]]) -> Any:
    """Run requests through the server's real ASGI app, lifespan included."""
    app = server.mcp.http_app(transport="streamable-http")

    async def go() -> Any:
        async with app.router.lifespan_context(app):
            transport = httpx.ASGITransport(app=app)
            async with httpx.AsyncClient(transport=transport, base_url=BASE_URL) as client:
                return await exchange(client)

    return asyncio.run(go())


def _initialize(server: ModuleType, authorization: str | None) -> httpx.Response:
    headers = dict(MCP_HEADERS)
    if authorization is not None:
        headers["Authorization"] = authorization
    return _drive(server, lambda client: client.post("/mcp", json=INITIALIZE, headers=headers))


def _jsonrpc(response: httpx.Response) -> dict[str, Any]:
    """The JSON-RPC message, whether it came back as JSON or as one SSE event."""
    if response.headers.get("content-type", "").startswith("text/event-stream"):
        data = [ln[5:].strip() for ln in response.text.splitlines() if ln.startswith("data:")]
        assert data, f"SSE response carried no data line: {response.text!r}"
        return json.loads(data[-1])
    return response.json()


# ============================================================
# 1. No token: HTTP refuses to start
# ============================================================


@pytest.mark.parametrize(
    "env",
    [
        pytest.param({}, id="token-unset-default-transport"),
        pytest.param({"MCP_TRANSPORT": "streamable-http"}, id="token-unset-explicit-http"),
        pytest.param({"MCP_AUTH_TOKEN": ""}, id="token-empty"),
        pytest.param({"MCP_AUTH_TOKEN": "   "}, id="token-whitespace"),
    ],
)
def test_http_without_a_token_refuses_to_load(load_server, capsys, env):
    with pytest.raises(SystemExit) as excinfo:
        load_server(**env)

    assert excinfo.value.code == 1
    cause = excinfo.value.__cause__
    assert isinstance(cause, AuthRequiredError), f"exited for another reason: {cause!r}"
    assert "Bearer token required for client 'searxng'" in str(cause)

    # The operator reads this in `docker logs`, so it has to name the variable.
    err = capsys.readouterr().err
    assert "FATAL: Bearer token required for client 'searxng'" in err
    assert "MCP_AUTH_TOKEN" in err


def test_auth_required_false_does_not_open_http(load_server, monkeypatch):
    """The second refusal, on its own.

    MCP_AUTH_REQUIRED=false gets the module past the first check with no auth
    provider. ``run_server`` must then refuse to serve it, because this server
    never passes ``allow_unauthenticated``. There is no setting that opens HTTP.
    """
    server = load_server(MCP_AUTH_REQUIRED="false")
    assert server.mcp.auth is None, "control: this test must reach run_server with no auth"
    served = _never_serve(server, monkeypatch)

    with pytest.raises(ValueError, match="Refusing to start streamable-http with no auth provider"):
        server.main()

    assert served == [], "the server went on to serve HTTP with no auth provider"


def test_stdio_starts_without_a_token(load_server, monkeypatch):
    """The one exemption: a local pipe, selected explicitly with MCP_TRANSPORT=stdio."""
    server = load_server(MCP_TRANSPORT="stdio")
    assert server.mcp.auth is None
    served = _never_serve(server, monkeypatch)

    server.main()

    assert served == [{"transport": "stdio"}]


def test_http_with_a_token_serves_http(load_server, monkeypatch):
    """Control for the refusals above: with a token, main() does reach the listener."""
    server = load_server(MCP_AUTH_TOKEN=TOKEN)
    assert server.mcp.auth is not None
    served = _never_serve(server, monkeypatch)

    server.main()

    assert [call["transport"] for call in served] == ["streamable-http"]
    assert served[0]["port"] == 3702


# ============================================================
# 2. With a token: 401 without it, 401 with a wrong one, served with the right one
# ============================================================


def test_initialize_without_a_header_is_401(load_server):
    response = _initialize(load_server(MCP_AUTH_TOKEN=TOKEN), None)
    assert response.status_code == 401
    assert response.headers.get("www-authenticate", "").lower().startswith("bearer")
    assert "mcp-session-id" not in response.headers, "a rejected request minted a session"


@pytest.mark.parametrize(
    "authorization",
    [
        pytest.param(f"Bearer {WRONG_TOKEN}", id="wrong-token"),
        pytest.param(f"Bearer {TOKEN}x", id="right-token-plus-one-character"),
        pytest.param(f"Basic {TOKEN}", id="right-token-wrong-scheme"),
        pytest.param("Bearer ", id="empty-bearer"),
    ],
)
def test_initialize_with_a_wrong_token_is_401(load_server, authorization):
    response = _initialize(load_server(MCP_AUTH_TOKEN=TOKEN), authorization)
    assert response.status_code == 401


def test_initialize_with_the_right_token_succeeds(load_server):
    response = _initialize(load_server(MCP_AUTH_TOKEN=TOKEN), f"Bearer {TOKEN}")

    assert response.status_code != 401
    assert response.status_code == 200, response.text
    message = _jsonrpc(response)
    assert "error" not in message, message
    assert message["result"]["serverInfo"]["name"] == "SearXNG Search"
    assert response.headers.get("mcp-session-id"), "initialize succeeded but opened no session"


def test_surrounding_whitespace_in_the_token_is_ignored(load_server):
    """A trailing newline from `echo token >> .env` must not lock every client out."""
    server = load_server(MCP_AUTH_TOKEN=f"  {TOKEN}\n")
    assert _initialize(server, f"Bearer {TOKEN}").status_code == 200
    assert _initialize(server, None).status_code == 401


# ============================================================
# 3. The container healthcheck stays green with auth on
# ============================================================


def _container_probe_path() -> str:
    """The path the Dockerfile HEALTHCHECK requests.

    It runs ``python -m mcp_searxng.healthcheck``, which resolves the path the
    way pete_mcp_core.healthcheck.main does. Neither the Dockerfile nor the
    compose file sets MCP_HEALTH_PATH (tests/test_healthcheck.py pins that), so
    this is the core default.
    """
    return os.getenv("MCP_HEALTH_PATH") or DEFAULT_HEALTH_PATH


def test_healthcheck_path_is_healthy_with_auth_on_and_no_token(load_server, monkeypatch):
    server = load_server(MCP_AUTH_TOKEN=TOKEN)
    monkeypatch.delenv("MCP_HEALTH_PATH", raising=False)
    path = _container_probe_path()
    healthy = _resolve_healthy_codes(os.getenv("MCP_HEALTH_CODES"))

    async def exchange(client: httpx.AsyncClient) -> tuple[httpx.Response, httpx.Response]:
        return await client.get(path), await client.get("/mcp", headers=MCP_HEADERS)

    probe, transport = _drive(server, exchange)

    assert path not in TRANSPORT_PATHS
    assert not path.startswith(("/mcp", "/sse"))
    assert probe.status_code in healthy, (
        f"unauthenticated probe of {path} returned {probe.status_code}, which the "
        f"healthcheck counts as unhealthy: the container would restart in a loop"
    )
    assert "mcp-session-id" not in probe.headers, "the probe minted a transport session"
    # Control: the same unauthenticated client IS turned away from the transport,
    # so the probe is not green merely because nothing is guarded.
    assert transport.status_code == 401


# ============================================================
# The same, against the real process and a real socket
# ============================================================


def _free_port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


@pytest.mark.parametrize(
    ("env", "refusal"),
    [
        pytest.param({}, "FATAL: Bearer token required for client 'searxng'", id="no-token"),
        pytest.param(
            {"MCP_AUTH_REQUIRED": "false"},
            "Refusing to start streamable-http with no auth provider",
            id="auth-required-false",
        ),
    ],
)
def test_real_process_exits_non_zero_without_a_token(tmp_path, env, refusal):
    """What the container does: `python -m mcp_searxng.server`, no token, exit."""
    port = _free_port()
    proc = subprocess.Popen(
        [sys.executable, "-m", MODULE],
        env=_clean_env({"FASTMCP_PORT": str(port), **env}),
        cwd=tmp_path,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.PIPE,
    )
    try:
        _, stderr = proc.communicate(timeout=30)
    except subprocess.TimeoutExpired:
        proc.kill()
        proc.communicate()
        pytest.fail("server kept running over HTTP with no token: it did not refuse to start")

    assert proc.returncode not in (0, None)
    assert refusal in stderr.decode(errors="replace")


@contextlib.contextmanager
def _serve(tmp_path, extra_env: dict[str, str]) -> Iterator[int]:
    port = _free_port()
    proc = subprocess.Popen(
        [sys.executable, "-m", MODULE],
        env=_clean_env({"FASTMCP_PORT": str(port), **extra_env}),
        cwd=tmp_path,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.PIPE,
    )
    try:
        deadline = time.monotonic() + 30
        while True:
            if proc.poll() is not None:
                err = proc.stderr.read().decode(errors="replace") if proc.stderr else ""
                pytest.fail(f"server exited early ({proc.returncode}):\n{err[-2000:]}")
            try:
                with socket.create_connection(("127.0.0.1", port), timeout=0.5):
                    break
            except OSError:
                if time.monotonic() > deadline:
                    pytest.fail("server did not start listening within 30s")
                time.sleep(0.1)
        yield port
    finally:
        proc.terminate()
        try:
            proc.wait(timeout=10)
        except subprocess.TimeoutExpired:
            proc.kill()
            proc.wait()


def _run_container_healthcheck(port: int) -> int:
    """Exactly the Dockerfile HEALTHCHECK command, pointed at ``port``."""
    result = subprocess.run(
        [sys.executable, "-m", HEALTHCHECK_MODULE],
        env=_clean_env({"FASTMCP_PORT": str(port)}),
        capture_output=True,
        timeout=30,
    )
    return result.returncode


def test_container_healthcheck_passes_against_a_real_authed_server(tmp_path):
    with _serve(tmp_path, {"MCP_AUTH_TOKEN": TOKEN}) as port:
        url = f"http://127.0.0.1:{port}/mcp"
        anonymous = httpx.post(url, json=INITIALIZE, headers=MCP_HEADERS)
        authed = httpx.post(
            url, json=INITIALIZE, headers={**MCP_HEADERS, "Authorization": f"Bearer {TOKEN}"}
        )
        healthcheck = _run_container_healthcheck(port)

    assert anonymous.status_code == 401
    assert authed.status_code == 200
    assert healthcheck == 0, "the tokenless healthcheck failed against an auth-enabled server"

    # Control: the healthcheck CAN fail. Nothing listens on this port any more.
    assert _run_container_healthcheck(port) == 1
