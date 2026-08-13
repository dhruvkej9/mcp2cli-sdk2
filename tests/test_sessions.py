"""Persistent session daemons must behave exactly like a direct connection.

The daemon has its own dispatch layer, so every fix that lands on the direct
path (pagination, content-block rendering, clean errors, --list shaping) has to
be proven here too rather than assumed.
"""

import json
import os
import subprocess
import sys
import time
from pathlib import Path

import pytest

import mcp2cli

HOSTILE = str(Path(__file__).parent / "hostile_server.py")
SERVER = f"{sys.executable} {HOSTILE} all"


@pytest.fixture
def session(tmp_path):
    """Start a session daemon against the hostile server; stop it afterwards."""
    env = {**os.environ, "MCP2CLI_CACHE_DIR": str(tmp_path / "cache")}
    name = "pytest-session"

    def cli(*args, timeout=60, stdin_data=None):
        return subprocess.run(
            [sys.executable, "-m", "mcp2cli", *args],
            capture_output=True, text=True, env=env, timeout=timeout, input=stdin_data,
        )

    started = cli("--mcp-stdio", SERVER, "--session-start", name, timeout=120)
    if started.returncode != 0:
        pytest.fail(f"session daemon did not start: {started.stdout}{started.stderr}")
    try:
        yield name, cli
    finally:
        cli("--session-stop", name)


class TestSessionParity:
    def test_list_follows_pagination(self, session):
        name, cli = session
        r = cli("--session", name, "--list", "--compact")
        assert r.returncode == 0, r.stderr
        names = r.stdout.split()
        assert "get-user" in names  # page 1
        assert "page-two-tool" in names  # page 2

    def test_colliding_names_are_deduped(self, session):
        name, cli = session
        r = cli("--session", name, "--list", "--compact")
        assert "get-user-2" in r.stdout.split()

    def test_tool_call(self, session):
        name, cli = session
        r = cli("--session", name, "get-user", "--id", "9")
        assert r.returncode == 0, r.stderr
        assert json.loads(r.stdout)["args"] == {"id": "9"}

    def test_structured_content_only(self, session):
        name, cli = session
        r = cli("--session", name, "structured-only")
        assert r.returncode == 0, r.stderr
        assert json.loads(r.stdout) == {"ok": True, "count": 42}

    def test_resource_link_and_embedded_resource(self, session):
        name, cli = session
        r = cli("--session", name, "links")
        assert r.returncode == 0, r.stderr
        assert "hostile://doc/one" in r.stdout
        assert "embedded body" in r.stdout

    def test_error_result_exits_non_zero(self, session):
        name, cli = session
        r = cli("--session", name, "boom")
        assert r.returncode == 1
        assert "tool exploded" in r.stderr
        assert "Traceback (most recent call last)" not in r.stderr

    def test_resources_paginate(self, session):
        name, cli = session
        r = cli("--session", name, "--list-resources")
        assert r.returncode == 0, r.stderr
        uris = [item["uri"] for item in json.loads(r.stdout)]
        assert "hostile://doc/one" in uris and "hostile://doc/two" in uris

    def test_prompts_paginate(self, session):
        name, cli = session
        r = cli("--session", name, "--list-prompts")
        assert r.returncode == 0, r.stderr
        names = [p["name"] for p in json.loads(r.stdout)]
        assert "first_prompt" in names and "second_prompt" in names

    def test_read_resource(self, session):
        name, cli = session
        r = cli("--session", name, "--read-resource", "hostile://doc/one")
        assert r.returncode == 0, r.stderr
        assert "page one body" in r.stdout


class TestSessionListOptions:
    """--compact / --top / --sort / --search / --json shape session output too."""

    def test_compact(self, session):
        name, cli = session
        r = cli("--session", name, "--list", "--compact")
        assert r.returncode == 0, r.stderr
        assert "\n" not in r.stdout.strip()

    def test_top_n(self, session):
        name, cli = session
        r = cli("--session", name, "--list", "--top", "3", "--compact")
        assert r.returncode == 0, r.stderr
        assert len(r.stdout.split()) == 3

    def test_search(self, session):
        name, cli = session
        r = cli("--session", name, "--search", "boom", "--compact")
        assert r.returncode == 0, r.stderr
        assert r.stdout.split() == ["boom"]

    def test_json_list(self, session):
        name, cli = session
        r = cli("--session", name, "--list", "--json")
        assert r.returncode == 0, r.stderr
        commands = json.loads(r.stdout)
        assert any(c["name"] == "get-user" for c in commands)

    def test_head_truncates_session_output(self, session):
        name, cli = session
        r = cli("--session", name, "--head", "1", "links")
        assert r.returncode == 0, r.stderr
        assert r.stdout.strip() == "see links"


class TestSessionLifecycle:
    def test_session_list_reports_the_daemon(self, session):
        name, cli = session
        r = cli("--session-list")
        assert r.returncode == 0, r.stderr
        assert name in r.stdout
        assert "alive" in r.stdout

    def test_unknown_session_is_a_clean_error(self, tmp_path):
        env = {**os.environ, "MCP2CLI_CACHE_DIR": str(tmp_path / "cache")}
        r = subprocess.run(
            [sys.executable, "-m", "mcp2cli", "--session", "nope", "--list"],
            capture_output=True, text=True, env=env, timeout=60,
        )
        assert r.returncode == 1
        assert "not found" in r.stderr
        assert "Traceback (most recent call last)" not in r.stderr


class TestSocketPath:
    """AF_UNIX paths are length-capped; a deep cache dir must still work."""

    def test_short_path_stays_in_the_cache_dir(self, monkeypatch, tmp_path):
        monkeypatch.setattr(mcp2cli, "SESSIONS_DIR", tmp_path)
        path = mcp2cli._session_sock_path("s")
        assert path.parent == tmp_path

    def test_long_path_falls_back_to_temp(self, monkeypatch, tmp_path):
        deep = tmp_path / ("d" * 60) / ("e" * 60) / ("f" * 60)
        monkeypatch.setattr(mcp2cli, "SESSIONS_DIR", deep)
        path = mcp2cli._session_sock_path("s")
        assert len(str(path).encode()) <= 100
        assert path.name.startswith("mcp2cli-")

    def test_fallback_path_is_deterministic(self, monkeypatch, tmp_path):
        deep = tmp_path / ("d" * 60) / ("e" * 60) / ("f" * 60)
        monkeypatch.setattr(mcp2cli, "SESSIONS_DIR", deep)
        assert mcp2cli._session_sock_path("s") == mcp2cli._session_sock_path("s")
        assert mcp2cli._session_sock_path("s") != mcp2cli._session_sock_path("t")
