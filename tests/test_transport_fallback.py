"""HTTP transport selection: streamable ⇄ SSE, and when *not* to fall back.

The old detection compared exceptions against ``httpx`` classes, but the MCP
SDK 2.0 raises ``httpx2``/``MCPError`` wrapped in nested anyio ``ExceptionGroup``s
— so the check was always false and SSE-only servers reached at a non-``/sse``
URL failed with a traceback instead of connecting.
"""

import json
import subprocess
import sys
import threading
from http.server import BaseHTTPRequestHandler, HTTPServer

import pytest

import mcp2cli


class _Handler(BaseHTTPRequestHandler):
    """Answers every MCP POST with the configured status."""

    status = 405
    body = b"Method Not Allowed"

    def do_POST(self):  # noqa: N802 - BaseHTTPRequestHandler API
        self.send_response(type(self).status)
        self.send_header("Content-Type", "text/plain")
        self.send_header("Content-Length", str(len(type(self).body)))
        self.end_headers()
        self.wfile.write(type(self).body)

    do_GET = do_POST

    def log_message(self, *args):
        pass


@pytest.fixture
def http_server():
    servers = []

    def start(status=405, body=b"Method Not Allowed"):
        handler = type("H", (_Handler,), {"status": status, "body": body})
        server = HTTPServer(("127.0.0.1", 0), handler)
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        servers.append(server)
        return f"http://127.0.0.1:{server.server_port}"

    yield start
    for server in servers:
        server.shutdown()


def run_cli(*args, timeout=60):
    return subprocess.run(
        [sys.executable, "-m", "mcp2cli", "--refresh", *args],
        capture_output=True,
        text=True,
        timeout=timeout,
    )


class TestTransportDetection:
    """Unit coverage — the decision itself, independent of any server."""

    def test_detects_405_status(self):
        response = type("R", (), {"status_code": 405})()
        exc = mcp2cli.httpx.HTTPStatusError("nope", request=None, response=response)
        assert mcp2cli._is_transport_unsupported(exc) is True

    def test_detects_404_status(self):
        response = type("R", (), {"status_code": 404})()
        exc = mcp2cli.httpx.HTTPStatusError("nope", request=None, response=response)
        assert mcp2cli._is_transport_unsupported(exc) is True

    def test_ignores_auth_failure(self):
        response = type("R", (), {"status_code": 401})()
        exc = mcp2cli.httpx.HTTPStatusError("nope", request=None, response=response)
        assert mcp2cli._is_transport_unsupported(exc) is False

    def test_ignores_server_error(self):
        response = type("R", (), {"status_code": 500})()
        exc = mcp2cli.httpx.HTTPStatusError("nope", request=None, response=response)
        assert mcp2cli._is_transport_unsupported(exc) is False

    def test_detects_connect_error(self):
        assert mcp2cli._is_transport_unsupported(mcp2cli.httpx.ConnectError("x")) is True

    def test_detects_method_not_found_inside_exception_group(self):
        """The shape the SDK actually raises for an SSE-only endpoint."""
        from mcp.shared.exceptions import MCPError

        inner = MCPError(code=-32601, message="Not Found")
        group = ExceptionGroup(
            "unhandled errors in a TaskGroup",
            [ExceptionGroup("unhandled errors in a TaskGroup", [inner])],
        )
        assert mcp2cli._is_transport_unsupported(group) is True

    def test_ignores_unrelated_error_in_group(self):
        group = ExceptionGroup("g", [ExceptionGroup("g", [ValueError("bad json")])])
        assert mcp2cli._is_transport_unsupported(group) is False

    def test_iter_exceptions_flattens_nesting(self):
        leaf = ValueError("leaf")
        group = ExceptionGroup("outer", [ExceptionGroup("inner", [leaf])])
        assert leaf in list(mcp2cli._iter_exceptions(group))

    def test_tool_errors_never_trigger_a_fallback(self):
        """A tool that fails after the handshake must not be run twice."""
        exc = mcp2cli._MCPCallError(f"{mcp2cli._TOOL_ERROR_MARKER}boom")
        assert mcp2cli._surface_tool_error_present(exc) is True


class TestUrlHeuristic:
    def test_sse_path_selects_sse(self):
        assert mcp2cli._resolve_transport("https://x.dev/sse", "auto") == "sse"

    def test_sse_path_with_trailing_slash(self):
        assert mcp2cli._resolve_transport("https://x.dev/sse/", "auto") == "sse"

    def test_mcp_path_stays_auto(self):
        assert mcp2cli._resolve_transport("https://x.dev/mcp", "auto") == "auto"

    def test_explicit_transport_is_respected(self):
        assert mcp2cli._resolve_transport("https://x.dev/sse", "streamable") == "streamable"


class TestEndToEndFallback:
    def test_405_endpoint_attempts_sse_fallback(self, http_server):
        """A 405 means 'no streamable here' — mcp2cli must retry over SSE."""
        url = http_server(status=405)
        r = run_cli("--mcp", f"{url}/mcp", "--list", timeout=90)
        assert r.returncode == 1
        assert "retrying over SSE" in r.stderr
        assert "Traceback (most recent call last)" not in r.stderr

    def test_401_endpoint_does_not_fall_back(self, http_server):
        """An auth failure must surface, not hide behind a second attempt."""
        url = http_server(status=401, body=b"Unauthorized")
        r = run_cli("--mcp", f"{url}/mcp", "--list", timeout=90)
        assert r.returncode == 1
        assert "retrying over SSE" not in r.stderr
        assert "Traceback (most recent call last)" not in r.stderr

    def test_forced_streamable_never_falls_back(self, http_server):
        url = http_server(status=405)
        r = run_cli("--mcp", f"{url}/mcp", "--transport", "streamable", "--list", timeout=90)
        assert r.returncode == 1
        assert "retrying over SSE" not in r.stderr

    def test_connection_refused_is_clean(self):
        r = run_cli("--mcp", "http://127.0.0.1:9/mcp", "--list", timeout=90)
        assert r.returncode == 1
        assert "Error:" in r.stderr
        assert "Traceback (most recent call last)" not in r.stderr


class TestHttpClientConfig:
    def test_timeout_helper_uses_configured_timeout(self, monkeypatch):
        monkeypatch.setattr(mcp2cli, "_TIMEOUT", 12.0)
        timeout = mcp2cli._http_timeout()
        assert timeout.read == 12.0

    def test_connect_budget_is_capped(self, monkeypatch):
        monkeypatch.setattr(mcp2cli, "_TIMEOUT", 600.0)
        timeout = mcp2cli._http_timeout()
        assert timeout.connect == 30.0
        assert timeout.read == 600.0

    def test_oauth_provider_is_accepted_by_our_http_client(self):
        """Regression: the SDK's providers are httpx2.Auth, not httpx.Auth."""
        provider = mcp2cli.build_oauth_provider(
            "https://example.com/mcp", client_id="id", client_secret="secret"
        )
        with mcp2cli.httpx.Client(auth=provider) as client:
            assert client is not None

    def test_mcp_http_client_carries_headers(self):
        client = mcp2cli._create_mcp_http_client({"X-Test": "1"})
        assert client.headers.get("X-Test") == "1"
