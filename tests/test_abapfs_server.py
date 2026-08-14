"""mcp2cli against an ABAP FS-style MCP server.

`tests/abapfs_mock_server.py` reproduces the wire contract of the MCP server in
marcellourbani/vscode_abap_remote_fs, which combines three things none of the
other fixtures do:

* a **stateful** streamable-HTTP session (``Mcp-Session-Id``), where dropping
  the header makes every request after ``initialize`` fail with 400 — the exact
  shape of "listing worked, tool calls fail";
* an ``Authorization`` header accepted **either** as ``Bearer <key>`` or bare;
* a JSON-RPC error body (``-32001``) delivered with a 401 status.

Its tool schemas are copied from the extension's real `languageModelTools`
entries, so the generated flags are the ones a user would actually type.
"""

import json
import os
import re
import subprocess
import sys
import time
from pathlib import Path

import pytest

SERVER = str(Path(__file__).parent / "abapfs_mock_server.py")
API_KEY = "abap-secret"


def _start(*extra):
    proc = subprocess.Popen(
        [sys.executable, SERVER, *extra],
        stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True,
    )
    deadline = time.time() + 30
    while time.time() < deadline:
        line = proc.stdout.readline()
        if not line:
            if proc.poll() is not None:
                pytest.fail(f"mock server exited: {proc.stderr.read()[:300]}")
            continue
        match = re.search(r"PORT=(\d+)", line)
        if match:
            return proc, f"http://127.0.0.1:{match.group(1)}/mcp"
    proc.kill()
    pytest.fail("mock server did not report a port")


@pytest.fixture(scope="module")
def abap_server():
    proc, url = _start()
    yield url
    proc.terminate()
    proc.wait(timeout=10)


@pytest.fixture(scope="module")
def open_abap_server():
    """The extension allows every request when no API key is configured."""
    proc, url = _start("--no-auth")
    yield url
    proc.terminate()
    proc.wait(timeout=10)


def run_cli(*args, cache_dir=None, timeout=90):
    env = dict(os.environ)
    if cache_dir:
        env["MCP2CLI_CACHE_DIR"] = str(cache_dir)
    return subprocess.run(
        [sys.executable, "-m", "mcp2cli", "--refresh", *args],
        capture_output=True, text=True, timeout=timeout, env=env,
    )


def auth(value=f"Bearer {API_KEY}"):
    return ["--auth-header", f"Authorization:{value}"]


def sent(result):
    return json.loads(result.stdout)["args"]


class TestDiscovery:
    def test_list_tools(self, abap_server, tmp_path):
        r = run_cli("--mcp", abap_server, *auth(), "--list", "--compact",
                    cache_dir=tmp_path)
        assert r.returncode == 0, r.stderr
        names = r.stdout.split()
        assert "search-abap-objects" in names
        assert "get-abap-object-lines" in names

    def test_list_json_exposes_parameters(self, abap_server, tmp_path):
        r = run_cli("--mcp", abap_server, *auth(), "--list", "--json",
                    cache_dir=tmp_path)
        assert r.returncode == 0, r.stderr
        commands = {c["name"]: c for c in json.loads(r.stdout)}
        params = {p["name"] for p in commands["search-abap-objects"]["parameters"]}
        assert {"pattern", "types", "max-results", "connection-id"} <= params

    def test_search_filters_tools(self, abap_server, tmp_path):
        r = run_cli("--mcp", abap_server, *auth(), "--search", "webgui", "--compact",
                    cache_dir=tmp_path)
        assert r.returncode == 0, r.stderr
        assert r.stdout.split() == ["get-sap-webgui-url"]


class TestToolCallsOverAStatefulSession:
    """Every call rides an Mcp-Session-Id minted during initialize."""

    def test_no_argument_tool(self, abap_server, tmp_path):
        r = run_cli("--mcp", abap_server, *auth(), "get-test-folder", cache_dir=tmp_path)
        assert r.returncode == 0, r.stderr
        assert sent(r) == {}

    def test_required_string_arguments(self, abap_server, tmp_path):
        r = run_cli("--mcp", abap_server, *auth(), "get-sap-webgui-url",
                    "--connection-id", "dev100", "--transaction", "SE16N",
                    cache_dir=tmp_path)
        assert r.returncode == 0, r.stderr
        assert sent(r) == {"connectionId": "dev100", "transaction": "SE16N"}

    def test_enum_array_argument(self, abap_server, tmp_path):
        r = run_cli("--mcp", abap_server, *auth(), "search-abap-objects",
                    "--pattern", "Z*", "--types", "CLAS,PROG",
                    "--connection-id", "dev100", cache_dir=tmp_path)
        assert r.returncode == 0, r.stderr
        assert sent(r)["types"] == ["CLAS", "PROG"]

    def test_json_array_argument(self, abap_server, tmp_path):
        r = run_cli("--mcp", abap_server, *auth(), "search-abap-objects",
                    "--pattern", "Z*", "--types", '["TABL"]',
                    "--connection-id", "dev100", cache_dir=tmp_path)
        assert r.returncode == 0, r.stderr
        assert sent(r)["types"] == ["TABL"]

    def test_whole_numbers_are_sent_as_integers(self, abap_server, tmp_path):
        """`"type": "number"` servers often validate with an int checker."""
        r = run_cli("--mcp", abap_server, *auth(), "search-abap-objects",
                    "--pattern", "Z*", "--types", "CLAS", "--connection-id", "dev100",
                    "--max-results", "5", cache_dir=tmp_path)
        assert r.returncode == 0, r.stderr
        assert repr(sent(r)["maxResults"]) == "5"

    def test_fractional_numbers_are_preserved(self, abap_server, tmp_path):
        r = run_cli("--mcp", abap_server, *auth(), "search-abap-object-lines",
                    "--object-name", "Z", "--search-string", "x",
                    "--connection-id", "dev100", "--max-objects", "2.5",
                    cache_dir=tmp_path)
        assert r.returncode == 0, r.stderr
        assert sent(r)["maxObjects"] == 2.5

    def test_boolean_true_and_false(self, abap_server, tmp_path):
        on = run_cli("--mcp", abap_server, *auth(), "search-abap-object-lines",
                     "--object-name", "Z", "--search-string", "^FORM", "--is-regexp",
                     "--connection-id", "dev100", cache_dir=tmp_path)
        assert on.returncode == 0, on.stderr
        assert sent(on)["isRegexp"] is True

        off = run_cli("--mcp", abap_server, *auth(), "search-abap-object-lines",
                      "--object-name", "Z", "--search-string", "x", "--no-is-regexp",
                      "--connection-id", "dev100", cache_dir=tmp_path)
        assert off.returncode == 0, off.stderr
        assert sent(off)["isRegexp"] is False

    def test_enum_choice_is_enforced(self, abap_server, tmp_path):
        r = run_cli("--mcp", abap_server, *auth(), "get-abap-object-lines",
                    "--object-name", "Z", "--object-type", "NOPE",
                    "--connection-id", "dev100", cache_dir=tmp_path)
        assert r.returncode != 0
        assert "invalid choice" in r.stderr

    def test_raw_json_escape_hatch(self, abap_server, tmp_path):
        r = run_cli("--mcp", abap_server, *auth(), "search-abap-objects",
                    "--json", '{"pattern": "Z*", "types": ["FUNC"], "connectionId": "d"}',
                    cache_dir=tmp_path)
        assert r.returncode == 0, r.stderr
        assert sent(r)["types"] == ["FUNC"]

    def test_server_side_validation_error_is_clean(self, abap_server, tmp_path):
        """Omitting a required argument must not produce a traceback."""
        r = run_cli("--mcp", abap_server, *auth(), "search-abap-objects",
                    "--pattern", "Z*", cache_dir=tmp_path)
        assert r.returncode == 1
        assert "Missing required" in r.stderr
        assert "Traceback (most recent call last)" not in r.stderr

    def test_json_envelope(self, abap_server, tmp_path):
        r = run_cli("--mcp", abap_server, *auth(), "--json", "get-test-folder",
                    cache_dir=tmp_path)
        assert r.returncode == 0, r.stderr
        assert json.loads(r.stdout)["isError"] is False


class TestCredentialFormats:
    def test_bearer_prefix(self, abap_server, tmp_path):
        r = run_cli("--mcp", abap_server, *auth(f"Bearer {API_KEY}"), "get-test-folder",
                    cache_dir=tmp_path)
        assert r.returncode == 0, r.stderr

    def test_bare_token(self, abap_server, tmp_path):
        """The extension accepts a bare key as well as `Bearer <key>`."""
        r = run_cli("--mcp", abap_server, *auth(API_KEY), "get-test-folder",
                    cache_dir=tmp_path)
        assert r.returncode == 0, r.stderr

    def test_token_from_env(self, abap_server, tmp_path):
        env = dict(os.environ, ABAP_KEY=f"Bearer {API_KEY}",
                   MCP2CLI_CACHE_DIR=str(tmp_path))
        r = subprocess.run(
            [sys.executable, "-m", "mcp2cli", "--refresh", "--mcp", abap_server,
             "--auth-header", "Authorization:env:ABAP_KEY", "get-test-folder"],
            capture_output=True, text=True, timeout=90, env=env,
        )
        assert r.returncode == 0, r.stderr

    def test_missing_credentials_on_list(self, abap_server, tmp_path):
        r = run_cli("--mcp", abap_server, "--list", cache_dir=tmp_path)
        assert r.returncode == 1
        assert "authentication failed" in r.stderr
        assert "Traceback (most recent call last)" not in r.stderr

    def test_missing_credentials_on_tool_call(self, abap_server, tmp_path):
        r = run_cli("--mcp", abap_server, "get-test-folder", cache_dir=tmp_path)
        assert r.returncode == 1
        assert "authentication failed" in r.stderr

    def test_wrong_key(self, abap_server, tmp_path):
        r = run_cli("--mcp", abap_server, *auth("Bearer nope"), "get-test-folder",
                    cache_dir=tmp_path)
        assert r.returncode == 1
        assert "authentication failed" in r.stderr

    def test_unauthenticated_server_needs_no_credentials(self, open_abap_server, tmp_path):
        r = run_cli("--mcp", open_abap_server, "get-test-folder", cache_dir=tmp_path)
        assert r.returncode == 0, r.stderr


class TestBakedAndSession:
    def test_baked_config(self, abap_server, tmp_path):
        env = dict(
            os.environ,
            MCP2CLI_CONFIG_DIR=str(tmp_path / "cfg"),
            MCP2CLI_CACHE_DIR=str(tmp_path / "cache"),
        )

        def cli(*args):
            return subprocess.run(
                [sys.executable, "-m", "mcp2cli", *args],
                capture_output=True, text=True, timeout=90, env=env,
            )

        created = cli("bake", "create", "abap", "--mcp", abap_server,
                      "--auth-header", f"Authorization:Bearer {API_KEY}")
        assert created.returncode == 0, created.stderr

        listed = cli("@abap", "--list", "--compact")
        assert listed.returncode == 0, listed.stderr
        assert "search-abap-objects" in listed.stdout.split()

        called = cli("@abap", "get-sap-webgui-url", "--connection-id", "dev100")
        assert called.returncode == 0, called.stderr
        assert json.loads(called.stdout)["args"]["connectionId"] == "dev100"

    def test_session_daemon(self, abap_server, tmp_path):
        env = dict(os.environ, MCP2CLI_CACHE_DIR=str(tmp_path / "cache"))

        def cli(*args, timeout=120):
            return subprocess.run(
                [sys.executable, "-m", "mcp2cli", *args],
                capture_output=True, text=True, timeout=timeout, env=env,
            )

        started = cli("--mcp", abap_server, "--auth-header",
                      f"Authorization:Bearer {API_KEY}", "--session-start", "abap")
        assert started.returncode == 0, started.stdout + started.stderr
        try:
            listed = cli("--session", "abap", "--list", "--compact")
            assert listed.returncode == 0, listed.stderr
            assert "get-test-folder" in listed.stdout.split()

            called = cli("--session", "abap", "get-sap-webgui-url",
                         "--connection-id", "dev100")
            assert called.returncode == 0, called.stderr
            assert "dev100" in called.stdout
        finally:
            cli("--session-stop", "abap")
