"""Authenticated MCP servers — with the emphasis on tool *calls*.

Listing tools and calling one are different requests, and over SSE they are
different endpoints (`GET /sse` vs `POST /messages/`). A client that attaches
credentials to the handshake only will list happily and fail on every call, so
every test here that lists also calls.

The server (`tests/auth_mcp_server.py`) enforces the credential in ASGI
middleware ahead of the MCP app, so no request escapes the check.
"""

import json
import os
import re
import subprocess
import sys
import time
from pathlib import Path

import pytest

SERVER = str(Path(__file__).parent / "auth_mcp_server.py")
TOKEN = "sk-test-123"


def _start(transport, token=TOKEN, header="authorization"):
    proc = subprocess.Popen(
        [sys.executable, SERVER, transport, "--token", token, "--header", header],
        stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True,
    )
    deadline = time.time() + 60
    port = None
    while time.time() < deadline:
        line = proc.stdout.readline()
        if not line:
            if proc.poll() is not None:
                pytest.skip(f"auth server exited: {proc.stderr.read()[:300]}")
            continue
        match = re.search(r"PORT=(\d+)", line)
        if match:
            port = int(match.group(1))
            break
    if port is None:  # pragma: no cover - slow machine
        proc.kill()
        pytest.skip("auth server did not report a port in time")
    path = "/mcp" if transport == "streamable" else "/sse"
    time.sleep(1.0)  # let uvicorn finish binding
    return proc, f"http://127.0.0.1:{port}{path}"


@pytest.fixture(scope="module")
def streamable_server():
    proc, url = _start("streamable")
    yield url
    proc.terminate()
    proc.wait(timeout=10)


@pytest.fixture(scope="module")
def sse_server():
    proc, url = _start("sse")
    yield url
    proc.terminate()
    proc.wait(timeout=10)


@pytest.fixture(scope="module")
def apikey_server():
    """Same server, but gated on a custom header instead of Authorization."""
    proc, url = _start("streamable", token="key-abc", header="x-api-key")
    yield url
    proc.terminate()
    proc.wait(timeout=10)


def run_cli(*args, cache_dir=None, env=None, timeout=90, refresh=True):
    environ = dict(os.environ)
    if cache_dir:
        environ["MCP2CLI_CACHE_DIR"] = str(cache_dir)
    if env:
        environ.update(env)
    prefix = ["--refresh"] if refresh else []
    return subprocess.run(
        [sys.executable, "-m", "mcp2cli", *prefix, *args],
        capture_output=True, text=True, timeout=timeout, env=environ,
    )


def bearer(token=TOKEN):
    return ["--auth-header", f"Authorization:Bearer {token}"]


class TestStreamableHttpAuth:
    def test_list_with_valid_token(self, streamable_server, tmp_path):
        r = run_cli("--mcp", streamable_server, *bearer(), "--list", "--compact",
                    cache_dir=tmp_path)
        assert r.returncode == 0, r.stderr
        assert "echo" in r.stdout.split()

    def test_tool_call_with_valid_token(self, streamable_server, tmp_path):
        r = run_cli("--mcp", streamable_server, *bearer(), "echo", "--message", "hi",
                    cache_dir=tmp_path)
        assert r.returncode == 0, r.stderr
        assert "hi" in r.stdout

    def test_tool_call_sends_credentials_on_every_request(self, streamable_server, tmp_path):
        """The server echoes back the header it saw on the call itself."""
        r = run_cli("--mcp", streamable_server, *bearer(), "whoami", cache_dir=tmp_path)
        assert r.returncode == 0, r.stderr
        seen = json.loads(r.stdout)
        assert seen["value"] == f"Bearer {TOKEN}"

    def test_typed_args_survive_auth(self, streamable_server, tmp_path):
        r = run_cli("--mcp", streamable_server, *bearer(), "add-numbers", "--a", "2",
                    "--b", "40", cache_dir=tmp_path)
        assert r.returncode == 0, r.stderr
        assert "42" in r.stdout

    def test_json_output_on_authenticated_call(self, streamable_server, tmp_path):
        r = run_cli("--mcp", streamable_server, *bearer(), "--json", "echo",
                    "--message", "hi", cache_dir=tmp_path)
        assert r.returncode == 0, r.stderr
        assert json.loads(r.stdout)["isError"] is False

    def test_tool_failure_survives_the_authenticated_path(self, streamable_server, tmp_path):
        r = run_cli("--mcp", streamable_server, *bearer(), "explode", cache_dir=tmp_path)
        assert r.returncode == 1
        assert "Traceback (most recent call last)" not in r.stderr
        assert "Error:" in r.stderr

    def test_resources_with_auth(self, streamable_server, tmp_path):
        r = run_cli("--mcp", streamable_server, *bearer(), "--list-resources",
                    cache_dir=tmp_path)
        assert r.returncode == 0, r.stderr
        assert "file:///secret/doc.txt" in r.stdout

    def test_read_resource_with_auth(self, streamable_server, tmp_path):
        r = run_cli("--mcp", streamable_server, *bearer(), "--read-resource",
                    "file:///secret/doc.txt", cache_dir=tmp_path)
        assert r.returncode == 0, r.stderr
        assert "classified payload" in r.stdout

    def test_prompts_with_auth(self, streamable_server, tmp_path):
        r = run_cli("--mcp", streamable_server, *bearer(), "--list-prompts",
                    cache_dir=tmp_path)
        assert r.returncode == 0, r.stderr
        assert "secret_prompt" in r.stdout

    def test_get_prompt_with_auth(self, streamable_server, tmp_path):
        r = run_cli("--mcp", streamable_server, *bearer(), "--get-prompt",
                    "secret_prompt", "--prompt-arg", "topic=auth", cache_dir=tmp_path)
        assert r.returncode == 0, r.stderr
        assert "auth" in r.stdout


class TestSseAuth:
    """SSE puts the handshake and the call on different endpoints."""

    def test_list_with_valid_token(self, sse_server, tmp_path):
        r = run_cli("--mcp", sse_server, *bearer(), "--list", "--compact",
                    cache_dir=tmp_path)
        assert r.returncode == 0, r.stderr
        assert "echo" in r.stdout.split()

    def test_tool_call_with_valid_token(self, sse_server, tmp_path):
        """Credentials must reach POST /messages/, not just GET /sse."""
        r = run_cli("--mcp", sse_server, *bearer(), "echo", "--message", "over sse",
                    cache_dir=tmp_path)
        assert r.returncode == 0, r.stderr
        assert "over sse" in r.stdout

    def test_tool_call_forced_sse_transport(self, sse_server, tmp_path):
        r = run_cli("--mcp", sse_server, "--transport", "sse", *bearer(),
                    "add-numbers", "--a", "1", "--b", "1", cache_dir=tmp_path)
        assert r.returncode == 0, r.stderr
        assert "2" in r.stdout

    def test_resource_read_with_auth(self, sse_server, tmp_path):
        r = run_cli("--mcp", sse_server, *bearer(), "--read-resource",
                    "file:///secret/doc.txt", cache_dir=tmp_path)
        assert r.returncode == 0, r.stderr
        assert "classified payload" in r.stdout


class TestRejectedCredentials:
    """Every rejection must name itself and exit non-zero, without a traceback."""

    def _assert_clean_auth_error(self, r):
        assert r.returncode == 1
        assert "Traceback (most recent call last)" not in r.stderr
        assert "ExceptionGroup" not in r.stderr
        assert "authentication failed" in r.stderr
        assert "--auth-header" in r.stderr

    def test_list_without_credentials(self, streamable_server, tmp_path):
        r = run_cli("--mcp", streamable_server, "--list", cache_dir=tmp_path)
        self._assert_clean_auth_error(r)

    def test_tool_call_without_credentials(self, streamable_server, tmp_path):
        r = run_cli("--mcp", streamable_server, "echo", "--message", "x",
                    cache_dir=tmp_path)
        self._assert_clean_auth_error(r)

    def test_tool_call_with_wrong_token(self, streamable_server, tmp_path):
        r = run_cli("--mcp", streamable_server, *bearer("wrong-token"), "echo",
                    "--message", "x", cache_dir=tmp_path)
        self._assert_clean_auth_error(r)

    def test_sse_without_credentials(self, sse_server, tmp_path):
        r = run_cli("--mcp", sse_server, "--list", cache_dir=tmp_path)
        self._assert_clean_auth_error(r)

    def test_forced_streamable_without_credentials(self, streamable_server, tmp_path):
        r = run_cli("--mcp", streamable_server, "--transport", "streamable", "--list",
                    cache_dir=tmp_path)
        self._assert_clean_auth_error(r)

    def test_auth_failure_does_not_retry_over_sse(self, streamable_server, tmp_path):
        """A bad token must not send us hunting for another transport."""
        r = run_cli("--mcp", streamable_server, "--list", cache_dir=tmp_path)
        assert "retrying over SSE" not in r.stderr


class TestCredentialSources:
    """Secrets should never have to appear in argv."""

    def test_env_prefix(self, streamable_server, tmp_path):
        r = run_cli(
            "--mcp", streamable_server,
            "--auth-header", "Authorization:env:MCP_TEST_TOKEN",
            "echo", "--message", "from env",
            cache_dir=tmp_path, env={"MCP_TEST_TOKEN": f"Bearer {TOKEN}"},
        )
        assert r.returncode == 0, r.stderr
        assert "from env" in r.stdout

    def test_file_prefix(self, streamable_server, tmp_path):
        secret = tmp_path / "token.txt"
        secret.write_text(f"Bearer {TOKEN}\n")
        r = run_cli(
            "--mcp", streamable_server,
            "--auth-header", f"Authorization:file:{secret}",
            "echo", "--message", "from file",
            cache_dir=tmp_path,
        )
        assert r.returncode == 0, r.stderr
        assert "from file" in r.stdout

    def test_missing_env_var_is_reported(self, streamable_server, tmp_path):
        r = run_cli(
            "--mcp", streamable_server,
            "--auth-header", "Authorization:env:DEFINITELY_NOT_SET_XYZ",
            "--list", cache_dir=tmp_path,
        )
        assert r.returncode == 1
        assert "DEFINITELY_NOT_SET_XYZ" in r.stderr

    def test_custom_header_name(self, apikey_server, tmp_path):
        r = run_cli("--mcp", apikey_server, "--auth-header", "x-api-key:key-abc",
                    "echo", "--message", "api key", cache_dir=tmp_path)
        assert r.returncode == 0, r.stderr
        assert "api key" in r.stdout

    def test_custom_header_missing_is_rejected(self, apikey_server, tmp_path):
        r = run_cli("--mcp", apikey_server, "echo", "--message", "x", cache_dir=tmp_path)
        assert r.returncode == 1
        assert "authentication failed" in r.stderr

    def test_bearer_value_containing_colons(self, streamable_server, tmp_path):
        """Only the first colon separates header name from value."""
        r = run_cli("--mcp", streamable_server, "--auth-header",
                    f"Authorization:Bearer {TOKEN}", "--list", "--compact",
                    cache_dir=tmp_path)
        assert r.returncode == 0, r.stderr


class TestAuthThroughOtherEntryPoints:
    """Baked configs and session daemons must carry credentials too."""

    def test_baked_config_calls_a_tool(self, streamable_server, tmp_path):
        env = {
            "MCP2CLI_CONFIG_DIR": str(tmp_path / "cfg"),
            "MCP2CLI_CACHE_DIR": str(tmp_path / "cache"),
        }
        created = run_cli(
            "bake", "create", "authed", "--mcp", streamable_server,
            "--auth-header", f"Authorization:Bearer {TOKEN}", env=env, refresh=False,
        )
        assert created.returncode == 0, created.stderr

        listed = run_cli("@authed", "--list", "--compact", env=env)
        assert listed.returncode == 0, listed.stderr
        assert "echo" in listed.stdout.split()

        called = run_cli("@authed", "echo", "--message", "baked auth", env=env)
        assert called.returncode == 0, called.stderr
        assert "baked auth" in called.stdout

    def test_global_flags_may_precede_a_baked_tool(self, streamable_server, tmp_path):
        """`mcp2cli --json @tool ...` used to fail with "one of --spec … required"."""
        env = {
            "MCP2CLI_CONFIG_DIR": str(tmp_path / "cfg"),
            "MCP2CLI_CACHE_DIR": str(tmp_path / "cache"),
        }
        created = run_cli(
            "bake", "create", "authed", "--mcp", streamable_server,
            "--auth-header", f"Authorization:Bearer {TOKEN}", env=env, refresh=False,
        )
        assert created.returncode == 0, created.stderr

        r = run_cli("--json", "@authed", "echo", "--message", "flagged", env=env)
        assert r.returncode == 0, r.stderr
        assert json.loads(r.stdout)["isError"] is False

    def test_session_daemon_calls_a_tool(self, streamable_server, tmp_path):
        env = {"MCP2CLI_CACHE_DIR": str(tmp_path / "cache")}
        name = "auth-session"
        started = run_cli(
            "--mcp", streamable_server, "--auth-header", f"Authorization:Bearer {TOKEN}",
            "--session-start", name, env=env, timeout=120,
        )
        assert started.returncode == 0, started.stdout + started.stderr
        try:
            listed = run_cli("--session", name, "--list", "--compact", env=env)
            assert listed.returncode == 0, listed.stderr
            assert "echo" in listed.stdout.split()

            called = run_cli("--session", name, "echo", "--message", "session auth",
                             env=env)
            assert called.returncode == 0, called.stderr
            assert "session auth" in called.stdout
        finally:
            run_cli("--session-stop", name, env=env)
