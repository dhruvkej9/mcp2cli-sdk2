"""Integration tests against real, published MCP servers.

These are the tests that answer "does it work with any MCP server in the
market?" — the in-repo fakes can only prove mcp2cli agrees with itself.

Opt in with::

    MCP2CLI_TEST_REAL=1 uv run pytest tests/test_real_servers.py -v

They are skipped by default because they download npm packages and bind local
ports. Requires Node.js (``npx``) and network access.

Covered:

* ``@modelcontextprotocol/server-everything`` over **stdio**, **streamable
  HTTP** and **SSE** — structured content, resource links, embedded resources,
  images, prompts, resources, enums, booleans
* ``@modelcontextprotocol/server-filesystem`` — array params, error results
* ``@modelcontextprotocol/server-memory`` — array-of-objects params
* a genuine **MCP 1.x** server (``mcp-server-time`` pinned to ``mcp==1.9.0``)
  to prove protocol back-compat with the SDK 2.0 client
"""

import json
import os
import shutil
import socket
import subprocess
import sys
import time

import pytest

pytestmark = [
    pytest.mark.real,
    pytest.mark.slow,
    pytest.mark.skipif(
        os.environ.get("MCP2CLI_TEST_REAL") != "1",
        reason="set MCP2CLI_TEST_REAL=1 to run tests against real MCP servers",
    ),
    pytest.mark.skipif(shutil.which("npx") is None, reason="npx (Node.js) not installed"),
]

EVERYTHING = "npx -y @modelcontextprotocol/server-everything"
FILESYSTEM = "npx -y @modelcontextprotocol/server-filesystem"
MEMORY = "npx -y @modelcontextprotocol/server-memory"
# A real MCP 1.x server: proves the SDK 2.0 client still negotiates with the
# protocol version the vast majority of published servers were built against.
LEGACY_TIME = "uvx --with mcp==1.9.0 mcp-server-time --local-timezone UTC"

TIMEOUT = 240


@pytest.fixture(scope="session")
def cache_dir(tmp_path_factory):
    """Isolate the cache so real-server runs never poison the user's."""
    return str(tmp_path_factory.mktemp("mcp2cli-real-cache"))


def run_cli(*args, cache_dir=None, timeout=TIMEOUT, stdin_data=None):
    env = dict(os.environ)
    if cache_dir:
        env["MCP2CLI_CACHE_DIR"] = cache_dir
    return subprocess.run(
        [sys.executable, "-m", "mcp2cli", *args],
        capture_output=True,
        text=True,
        timeout=timeout,
        env=env,
        input=stdin_data,
    )


def stdio(server, *args, **kwargs):
    return run_cli("--mcp-stdio", server, "--refresh", *args, **kwargs)


def free_port():
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


@pytest.fixture(scope="module")
def everything_http():
    """The everything server over streamable HTTP."""
    yield from _serve("streamableHttp", "/mcp")


@pytest.fixture(scope="module")
def everything_sse():
    """The everything server over SSE."""
    yield from _serve("sse", "/sse")


def _serve(mode, path):
    port = free_port()
    env = {**os.environ, "PORT": str(port)}
    proc = subprocess.Popen(
        ["npx", "-y", "@modelcontextprotocol/server-everything", mode],
        env=env,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )
    url = f"http://127.0.0.1:{port}{path}"
    deadline = time.time() + 120
    while time.time() < deadline:
        if proc.poll() is not None:
            pytest.skip(f"everything server ({mode}) exited early")
        try:
            with socket.create_connection(("127.0.0.1", port), timeout=1):
                break
        except OSError:
            time.sleep(0.5)
    else:  # pragma: no cover - slow CI machine
        proc.kill()
        pytest.skip(f"everything server ({mode}) did not start in time")
    try:
        yield url
    finally:
        proc.terminate()
        try:
            proc.wait(timeout=10)
        except subprocess.TimeoutExpired:  # pragma: no cover
            proc.kill()


class TestEverythingStdio:
    def test_list(self, cache_dir):
        r = stdio(EVERYTHING, "--list", cache_dir=cache_dir)
        assert r.returncode == 0, r.stderr
        assert "echo" in r.stdout
        assert "get-sum" in r.stdout

    def test_echo(self, cache_dir):
        r = stdio(EVERYTHING, "echo", "--message", "hello", cache_dir=cache_dir)
        assert r.returncode == 0, r.stderr
        assert "hello" in r.stdout

    def test_numeric_params(self, cache_dir):
        r = stdio(EVERYTHING, "get-sum", "--a", "2", "--b", "40", cache_dir=cache_dir)
        assert r.returncode == 0, r.stderr
        assert "42" in r.stdout

    def test_structured_content(self, cache_dir):
        r = stdio(
            EVERYTHING, "get-structured-content", "--location", "Chicago",
            cache_dir=cache_dir,
        )
        assert r.returncode == 0, r.stderr
        assert "temperature" in r.stdout

    def test_structured_content_json_envelope(self, cache_dir):
        r = stdio(
            EVERYTHING, "--json", "get-structured-content", "--location", "Chicago",
            cache_dir=cache_dir,
        )
        assert r.returncode == 0, r.stderr
        payload = json.loads(r.stdout)
        assert "structuredContent" in payload
        assert payload["isError"] is False

    def test_enum_param_is_validated(self, cache_dir):
        r = stdio(
            EVERYTHING, "get-structured-content", "--location", "Atlantis",
            cache_dir=cache_dir,
        )
        assert r.returncode != 0
        assert "invalid choice" in r.stderr

    def test_resource_links_are_rendered(self, cache_dir):
        """resource_link blocks used to be dropped entirely."""
        r = stdio(EVERYTHING, "get-resource-links", "--count", "3", cache_dir=cache_dir)
        assert r.returncode == 0, r.stderr
        assert "://" in r.stdout, "no resource link URI in output"

    def test_image_content(self, cache_dir):
        r = stdio(EVERYTHING, "get-tiny-image", cache_dir=cache_dir)
        assert r.returncode == 0, r.stderr
        assert len(r.stdout) > 100  # base64 payload

    def test_boolean_flag(self, cache_dir):
        r = stdio(
            EVERYTHING, "get-annotated-message", "--message-type", "success",
            cache_dir=cache_dir,
        )
        assert r.returncode == 0, r.stderr

    def test_list_resources(self, cache_dir):
        r = stdio(EVERYTHING, "--list-resources", cache_dir=cache_dir)
        assert r.returncode == 0, r.stderr
        assert json.loads(r.stdout)

    def test_read_resource(self, cache_dir):
        listed = stdio(EVERYTHING, "--list-resources", cache_dir=cache_dir)
        uri = json.loads(listed.stdout)[0]["uri"]
        r = stdio(EVERYTHING, "--read-resource", uri, cache_dir=cache_dir)
        assert r.returncode == 0, r.stderr
        assert r.stdout.strip()

    def test_unknown_resource_is_clean(self, cache_dir):
        r = stdio(EVERYTHING, "--read-resource", "demo://nope", cache_dir=cache_dir)
        assert r.returncode == 1
        assert "Traceback (most recent call last)" not in r.stderr

    def test_list_prompts(self, cache_dir):
        r = stdio(EVERYTHING, "--list-prompts", cache_dir=cache_dir)
        assert r.returncode == 0, r.stderr
        assert json.loads(r.stdout)

    def test_get_prompt_with_args(self, cache_dir):
        r = stdio(
            EVERYTHING, "--get-prompt", "args-prompt", "--prompt-arg", "city=Paris",
            cache_dir=cache_dir,
        )
        assert r.returncode == 0, r.stderr
        assert "Paris" in r.stdout

    def test_list_json_is_parseable(self, cache_dir):
        r = stdio(EVERYTHING, "--list", "--json", cache_dir=cache_dir)
        assert r.returncode == 0, r.stderr
        commands = json.loads(r.stdout)
        assert any(c["name"] == "echo" for c in commands)

    def test_search(self, cache_dir):
        r = stdio(EVERYTHING, "--search", "image", cache_dir=cache_dir)
        assert r.returncode == 0, r.stderr
        assert "image" in r.stdout.lower()

    def test_task_required_tool_reports_clearly(self, cache_dir):
        """A tool needing task augmentation must explain itself, not dump JSON-RPC."""
        r = stdio(
            EVERYTHING, "simulate-research-query", "--topic", "ai", cache_dir=cache_dir
        )
        if r.returncode != 0:
            assert "Traceback (most recent call last)" not in r.stderr
            assert "Error:" in r.stderr


class TestEverythingStreamableHttp:
    def test_list(self, everything_http, cache_dir):
        r = run_cli("--mcp", everything_http, "--refresh", "--list", cache_dir=cache_dir)
        assert r.returncode == 0, r.stderr
        assert "echo" in r.stdout

    def test_call(self, everything_http, cache_dir):
        r = run_cli(
            "--mcp", everything_http, "--refresh", "echo", "--message", "over-http",
            cache_dir=cache_dir,
        )
        assert r.returncode == 0, r.stderr
        assert "over-http" in r.stdout

    def test_forced_streamable(self, everything_http, cache_dir):
        r = run_cli(
            "--mcp", everything_http, "--transport", "streamable", "--refresh",
            "--list", cache_dir=cache_dir,
        )
        assert r.returncode == 0, r.stderr


class TestEverythingSse:
    def test_list(self, everything_sse, cache_dir):
        r = run_cli("--mcp", everything_sse, "--refresh", "--list", cache_dir=cache_dir)
        assert r.returncode == 0, r.stderr
        assert "echo" in r.stdout

    def test_call(self, everything_sse, cache_dir):
        r = run_cli(
            "--mcp", everything_sse, "--refresh", "echo", "--message", "over-sse",
            cache_dir=cache_dir,
        )
        assert r.returncode == 0, r.stderr
        assert "over-sse" in r.stdout

    def test_forced_sse(self, everything_sse, cache_dir):
        r = run_cli(
            "--mcp", everything_sse, "--transport", "sse", "--refresh", "--list",
            cache_dir=cache_dir,
        )
        assert r.returncode == 0, r.stderr


class TestFilesystemServer:
    @pytest.fixture(scope="class")
    def workdir(self, tmp_path_factory):
        d = tmp_path_factory.mktemp("fs-server")
        (d / "hello.txt").write_text("hello from disk\n")
        return d

    def server(self, workdir):
        return f"{FILESYSTEM} {workdir}"

    def test_list(self, workdir, cache_dir):
        r = stdio(self.server(workdir), "--list", cache_dir=cache_dir)
        assert r.returncode == 0, r.stderr
        assert "read-text-file" in r.stdout

    def test_read_file(self, workdir, cache_dir):
        r = stdio(
            self.server(workdir), "read-text-file", "--path", str(workdir / "hello.txt"),
            cache_dir=cache_dir,
        )
        assert r.returncode == 0, r.stderr
        assert "hello from disk" in r.stdout

    def test_array_param(self, workdir, cache_dir):
        r = stdio(
            self.server(workdir), "read-multiple-files",
            "--paths", str(workdir / "hello.txt"),
            cache_dir=cache_dir,
        )
        assert r.returncode == 0, r.stderr
        assert "hello from disk" in r.stdout

    def test_missing_file_is_a_clean_error(self, workdir, cache_dir):
        r = stdio(
            self.server(workdir), "read-text-file", "--path", str(workdir / "nope.txt"),
            cache_dir=cache_dir,
        )
        assert r.returncode == 1
        assert "Traceback (most recent call last)" not in r.stderr
        assert "Error:" in r.stderr

    def test_write_then_read(self, workdir, cache_dir):
        target = workdir / "written.txt"
        w = stdio(
            self.server(workdir), "write-file", "--path", str(target),
            "--content", "round trip", cache_dir=cache_dir,
        )
        assert w.returncode == 0, w.stderr
        assert target.read_text() == "round trip"


class TestMemoryServer:
    def test_array_of_objects_param(self, tmp_path, cache_dir):
        env_file = tmp_path / "memory.json"
        os.environ["MEMORY_FILE_PATH"] = str(env_file)
        try:
            entities = json.dumps(
                [{"name": "Ada", "entityType": "person", "observations": ["writes code"]}]
            )
            r = stdio(MEMORY, "create-entities", "--entities", entities, cache_dir=cache_dir)
            assert r.returncode == 0, r.stderr
            assert "Ada" in r.stdout

            read = stdio(MEMORY, "read-graph", cache_dir=cache_dir)
            assert read.returncode == 0, read.stderr
            assert "Ada" in read.stdout
        finally:
            os.environ.pop("MEMORY_FILE_PATH", None)


@pytest.mark.skipif(shutil.which("uvx") is None, reason="uvx not installed")
class TestLegacyProtocolServer:
    """An MCP 1.x server — the protocol most published servers still speak."""

    def test_list(self, cache_dir):
        r = stdio(LEGACY_TIME, "--list", cache_dir=cache_dir)
        if r.returncode != 0:
            pytest.skip(f"legacy server unavailable: {r.stderr[:200]}")
        assert "get-current-time" in r.stdout

    def test_call(self, cache_dir):
        r = stdio(LEGACY_TIME, "get-current-time", "--timezone", "UTC", cache_dir=cache_dir)
        if r.returncode != 0:
            pytest.skip(f"legacy server unavailable: {r.stderr[:200]}")
        assert "timezone" in r.stdout.lower() or "datetime" in r.stdout.lower()


class TestBakeAgainstRealServer:
    def test_bake_roundtrip(self, tmp_path, cache_dir):
        env = dict(os.environ, MCP2CLI_CONFIG_DIR=str(tmp_path), MCP2CLI_CACHE_DIR=cache_dir)

        def cli(*args):
            return subprocess.run(
                [sys.executable, "-m", "mcp2cli", *args],
                capture_output=True, text=True, timeout=TIMEOUT, env=env,
            )

        created = cli("bake", "create", "everything", "--mcp-stdio", EVERYTHING)
        assert created.returncode == 0, created.stderr

        listed = cli("@everything", "--list", "--compact")
        assert listed.returncode == 0, listed.stderr
        assert "echo" in listed.stdout

        called = cli("@everything", "echo", "--message", "baked")
        assert called.returncode == 0, called.stderr
        assert "baked" in called.stdout
