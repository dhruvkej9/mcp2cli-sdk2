"""Tests for bake mode — configuration CRUD and filtered execution."""

import json
import os
import subprocess
import sys
from pathlib import Path

import pytest

from mcp2cli import (
    BakeConfig,
    CommandDef,
    filter_commands,
    _load_baked_all,
    _save_baked_all,
    _load_baked,
    _baked_to_argv,
    _BAKE_NAME_RE,
    _parser_branding,
)

MCP_SERVER = str(Path(__file__).parent / "mcp_test_server.py")


# ---------------------------------------------------------------------------
# Unit tests: filter_commands
# ---------------------------------------------------------------------------


class TestFilterCommands:
    def _cmds(self):
        return [
            CommandDef(name="list-pets", method="GET"),
            CommandDef(name="create-pet", method="POST"),
            CommandDef(name="delete-pet", method="DELETE"),
            CommandDef(name="update-pet", method="PUT"),
            CommandDef(name="echo", tool_name="echo"),  # MCP, no method
        ]

    def test_no_filters(self):
        cmds = self._cmds()
        assert filter_commands(cmds) == cmds

    def test_methods_filter(self):
        result = filter_commands(self._cmds(), methods=["GET", "POST"])
        names = [c.name for c in result]
        assert "list-pets" in names
        assert "create-pet" in names
        assert "delete-pet" not in names
        assert "update-pet" not in names
        # MCP commands (no method) pass through
        assert "echo" in names

    def test_include_filter(self):
        result = filter_commands(self._cmds(), include=["list-*"])
        assert [c.name for c in result] == ["list-pets"]

    def test_exclude_filter(self):
        result = filter_commands(self._cmds(), exclude=["delete-*", "update-*"])
        names = [c.name for c in result]
        assert "list-pets" in names
        assert "create-pet" in names
        assert "delete-pet" not in names
        assert "update-pet" not in names

    def test_combined_filters(self):
        # methods=GET,POST + exclude delete-* => list-pets, create-pet, echo
        result = filter_commands(
            self._cmds(), methods=["GET", "POST"], exclude=["create-*"]
        )
        names = [c.name for c in result]
        assert names == ["list-pets", "echo"]

    def test_include_and_exclude(self):
        # include *-pet, exclude delete-*
        result = filter_commands(
            self._cmds(), include=["*-pet"], exclude=["delete-*"]
        )
        names = [c.name for c in result]
        assert "create-pet" in names
        assert "update-pet" in names
        assert "delete-pet" not in names
        assert "list-pets" not in names  # list-pets not matched by *-pet

    def test_case_insensitive_methods(self):
        result = filter_commands(self._cmds(), methods=["get"])
        names = [c.name for c in result]
        assert "list-pets" in names


# ---------------------------------------------------------------------------
# Unit tests: config CRUD
# ---------------------------------------------------------------------------


class TestConfigCRUD:
    def test_round_trip(self, tmp_path, monkeypatch):
        import mcp2cli

        monkeypatch.setattr(mcp2cli, "BAKED_FILE", tmp_path / "baked.json")
        data = {
            "test": {
                "source_type": "spec",
                "source": "https://example.com/spec.json",
                "include": [],
                "exclude": [],
                "methods": [],
            }
        }
        _save_baked_all(data)
        loaded = _load_baked_all()
        assert loaded == data

    def test_load_missing(self, tmp_path, monkeypatch):
        import mcp2cli

        monkeypatch.setattr(mcp2cli, "BAKED_FILE", tmp_path / "nope.json")
        assert _load_baked_all() == {}

    def test_load_single(self, tmp_path, monkeypatch):
        import mcp2cli

        monkeypatch.setattr(mcp2cli, "BAKED_FILE", tmp_path / "baked.json")
        data = {"foo": {"source_type": "mcp", "source": "http://x"}}
        _save_baked_all(data)
        assert _load_baked("foo") == data["foo"]
        assert _load_baked("bar") is None


# ---------------------------------------------------------------------------
# Unit tests: name validation
# ---------------------------------------------------------------------------


class TestNameValidation:
    def test_valid_names(self):
        for name in ["petstore", "my-api", "a1", "x-y-z"]:
            assert _BAKE_NAME_RE.match(name), f"{name} should be valid"

    def test_invalid_names(self):
        for name in ["1abc", "Abc", "a_b", "-foo", ""]:
            assert not _BAKE_NAME_RE.match(name), f"{name} should be invalid"


# ---------------------------------------------------------------------------
# Unit tests: _baked_to_argv
# ---------------------------------------------------------------------------


class TestBakedToArgv:
    def test_spec_mode(self):
        cfg = {
            "source_type": "spec",
            "source": "https://example.com/spec.json",
            "base_url": "https://api.example.com",
            "auth_headers": [["Authorization", "env:TOKEN"]],
            "env_vars": {},
            "cache_ttl": 7200,
            "transport": "auto",
            "oauth": False,
        }
        argv = _baked_to_argv(cfg)
        assert "--spec" in argv
        assert "https://example.com/spec.json" in argv
        assert "--base-url" in argv
        assert "--auth-header" in argv
        assert "Authorization:env:TOKEN" in argv
        assert "--cache-ttl" in argv
        assert "7200" in argv

    def test_mcp_stdio_mode(self):
        cfg = {
            "source_type": "mcp_stdio",
            "source": "npx @mcp/github",
            "auth_headers": [],
            "env_vars": {"GH_TOKEN": "abc"},
            "cache_ttl": 3600,
            "transport": "auto",
            "oauth": False,
        }
        argv = _baked_to_argv(cfg)
        assert "--mcp-stdio" in argv
        assert "npx @mcp/github" in argv
        assert "--env" in argv
        assert "GH_TOKEN=abc" in argv

    def test_oauth_flags(self):
        cfg = {
            "source_type": "mcp",
            "source": "https://mcp.example.com",
            "auth_headers": [],
            "env_vars": {},
            "cache_ttl": 3600,
            "transport": "sse",
            "oauth": True,
            "oauth_client_id": "env:CID",
            "oauth_client_secret": "env:CSEC",
            "oauth_scope": "read write",
        }
        argv = _baked_to_argv(cfg)
        assert "--oauth" in argv
        assert "--oauth-client-id" in argv
        assert "--oauth-client-secret" in argv
        assert "--oauth-scope" in argv
        assert "--transport" in argv
        assert "sse" in argv
        assert "--oauth-redirect-uri" not in argv

    def test_oauth_redirect_uri_roundtrip(self):
        custom_uri = "http://localhost:18080/oauth/callback"
        cfg = {
            "source_type": "mcp",
            "source": "https://mcp.example.com",
            "auth_headers": [],
            "env_vars": {},
            "cache_ttl": 3600,
            "transport": "auto",
            "oauth": True,
            "oauth_client_id": None,
            "oauth_client_secret": None,
            "oauth_scope": None,
            "oauth_redirect_uri": custom_uri,
        }
        argv = _baked_to_argv(cfg)
        assert "--oauth-redirect-uri" in argv
        idx = argv.index("--oauth-redirect-uri")
        assert argv[idx + 1] == custom_uri

    def test_oauth_redirect_uri_none_omitted(self):
        cfg = {
            "source_type": "mcp",
            "source": "https://mcp.example.com",
            "auth_headers": [],
            "env_vars": {},
            "cache_ttl": 3600,
            "transport": "auto",
            "oauth": True,
            "oauth_redirect_uri": None,
        }
        argv = _baked_to_argv(cfg)
        assert "--oauth-redirect-uri" not in argv


# ---------------------------------------------------------------------------
# Integration tests (subprocess-based)
# ---------------------------------------------------------------------------


def _run(*args, config_dir=None, cache_dir=None):
    env = os.environ.copy()
    if config_dir:
        env["MCP2CLI_CONFIG_DIR"] = str(config_dir)
    if cache_dir:
        env["MCP2CLI_CACHE_DIR"] = str(cache_dir)
    cmd = [sys.executable, "-m", "mcp2cli", *args]
    return subprocess.run(cmd, capture_output=True, text=True, timeout=30, env=env)


class TestBakeCreateAndUse:
    def test_create_and_list(self, tmp_path):
        cfg_dir = tmp_path / "config"
        cache_dir = tmp_path / "cache"
        r = _run(
            "bake", "create", "test-echo",
            "--mcp-stdio", f"{sys.executable} {MCP_SERVER}",
            config_dir=cfg_dir, cache_dir=cache_dir,
        )
        assert r.returncode == 0
        assert "created" in r.stdout

        # bake list
        r = _run("bake", "list", config_dir=cfg_dir, cache_dir=cache_dir)
        assert r.returncode == 0
        assert "test-echo" in r.stdout

    def test_create_duplicate_fails(self, tmp_path):
        cfg_dir = tmp_path / "config"
        cache_dir = tmp_path / "cache"
        _run(
            "bake", "create", "dup",
            "--mcp-stdio", f"{sys.executable} {MCP_SERVER}",
            config_dir=cfg_dir, cache_dir=cache_dir,
        )
        r = _run(
            "bake", "create", "dup",
            "--mcp-stdio", f"{sys.executable} {MCP_SERVER}",
            config_dir=cfg_dir, cache_dir=cache_dir,
        )
        assert r.returncode != 0
        assert "already exists" in r.stderr

    def test_create_force_overwrite(self, tmp_path):
        cfg_dir = tmp_path / "config"
        cache_dir = tmp_path / "cache"
        _run(
            "bake", "create", "dup",
            "--mcp-stdio", f"{sys.executable} {MCP_SERVER}",
            config_dir=cfg_dir, cache_dir=cache_dir,
        )
        r = _run(
            "bake", "create", "dup", "--force",
            "--mcp-stdio", f"{sys.executable} {MCP_SERVER}",
            config_dir=cfg_dir, cache_dir=cache_dir,
        )
        assert r.returncode == 0

    def test_show(self, tmp_path):
        cfg_dir = tmp_path / "config"
        cache_dir = tmp_path / "cache"
        _run(
            "bake", "create", "showme",
            "--mcp-stdio", f"{sys.executable} {MCP_SERVER}",
            "--exclude", "deploy",
            config_dir=cfg_dir, cache_dir=cache_dir,
        )
        r = _run("bake", "show", "showme", config_dir=cfg_dir, cache_dir=cache_dir)
        assert r.returncode == 0
        data = json.loads(r.stdout)
        assert data["source_type"] == "mcp_stdio"
        assert "deploy" in data["exclude"]

    def test_remove(self, tmp_path):
        cfg_dir = tmp_path / "config"
        cache_dir = tmp_path / "cache"
        _run(
            "bake", "create", "removeme",
            "--mcp-stdio", f"{sys.executable} {MCP_SERVER}",
            config_dir=cfg_dir, cache_dir=cache_dir,
        )
        r = _run("bake", "remove", "removeme", config_dir=cfg_dir, cache_dir=cache_dir)
        assert r.returncode == 0
        assert "removed" in r.stdout.lower()

        r = _run("bake", "list", config_dir=cfg_dir, cache_dir=cache_dir)
        assert "removeme" not in r.stdout

    def test_update(self, tmp_path):
        cfg_dir = tmp_path / "config"
        cache_dir = tmp_path / "cache"
        _run(
            "bake", "create", "upd",
            "--mcp-stdio", f"{sys.executable} {MCP_SERVER}",
            config_dir=cfg_dir, cache_dir=cache_dir,
        )
        r = _run(
            "bake", "update", "upd", "--cache-ttl", "9999",
            config_dir=cfg_dir, cache_dir=cache_dir,
        )
        assert r.returncode == 0

        r = _run("bake", "show", "upd", config_dir=cfg_dir, cache_dir=cache_dir)
        data = json.loads(r.stdout)
        assert data["cache_ttl"] == 9999

    def test_run_baked_list(self, tmp_path):
        cfg_dir = tmp_path / "config"
        cache_dir = tmp_path / "cache"
        _run(
            "bake", "create", "mytools",
            "--mcp-stdio", f"{sys.executable} {MCP_SERVER}",
            config_dir=cfg_dir, cache_dir=cache_dir,
        )
        r = _run("@mytools", "--list", config_dir=cfg_dir, cache_dir=cache_dir)
        assert r.returncode == 0
        assert "echo" in r.stdout
        assert "add-numbers" in r.stdout

    def test_run_baked_execute(self, tmp_path):
        cfg_dir = tmp_path / "config"
        cache_dir = tmp_path / "cache"
        _run(
            "bake", "create", "mytools",
            "--mcp-stdio", f"{sys.executable} {MCP_SERVER}",
            config_dir=cfg_dir, cache_dir=cache_dir,
        )
        r = _run(
            "@mytools", "echo", "--message", "hello",
            config_dir=cfg_dir, cache_dir=cache_dir,
        )
        assert r.returncode == 0
        assert "hello" in r.stdout

    def test_run_baked_with_include_filter(self, tmp_path):
        cfg_dir = tmp_path / "config"
        cache_dir = tmp_path / "cache"
        _run(
            "bake", "create", "filtered",
            "--mcp-stdio", f"{sys.executable} {MCP_SERVER}",
            "--include", "echo",
            config_dir=cfg_dir, cache_dir=cache_dir,
        )
        r = _run("@filtered", "--list", config_dir=cfg_dir, cache_dir=cache_dir)
        assert r.returncode == 0
        assert "echo" in r.stdout
        assert "add-numbers" not in r.stdout

    def test_run_baked_with_exclude_filter(self, tmp_path):
        cfg_dir = tmp_path / "config"
        cache_dir = tmp_path / "cache"
        _run(
            "bake", "create", "no-deploy",
            "--mcp-stdio", f"{sys.executable} {MCP_SERVER}",
            "--exclude", "deploy",
            config_dir=cfg_dir, cache_dir=cache_dir,
        )
        r = _run("@no-deploy", "--list", config_dir=cfg_dir, cache_dir=cache_dir)
        assert r.returncode == 0
        assert "echo" in r.stdout
        assert "deploy" not in r.stdout

    def test_run_baked_nonexistent(self, tmp_path):
        cfg_dir = tmp_path / "config"
        cache_dir = tmp_path / "cache"
        r = _run("@nope", "--list", config_dir=cfg_dir, cache_dir=cache_dir)
        assert r.returncode != 0
        assert "no baked tool" in r.stderr

    def test_parser_branding_from_bake_config(self):
        assert _parser_branding(None)[0] == "mcp2cli"
        cfg = BakeConfig(prog="petstore", description="petstore cli")
        assert _parser_branding(cfg) == ("petstore", "petstore cli")
        cfg_default_desc = BakeConfig(prog="petstore")
        prog, description = _parser_branding(cfg_default_desc)
        assert prog == "petstore"
        assert "Turn any MCP server" in description

    def test_baked_help_uses_name_and_description(self, tmp_path, petstore_server):
        cfg_dir = tmp_path / "config"
        cache_dir = tmp_path / "cache"
        r = _run(
            "bake",
            "create",
            "petstore",
            "--description",
            "petstore cli",
            "--spec",
            f"{petstore_server}/openapi.json",
            config_dir=cfg_dir,
            cache_dir=cache_dir,
        )
        assert r.returncode == 0

        r = _run("@petstore", "-h", config_dir=cfg_dir, cache_dir=cache_dir)
        assert r.returncode == 0
        assert "usage: petstore" in r.stdout
        assert "petstore cli" in r.stdout

    def test_invalid_name_rejected(self, tmp_path):
        cfg_dir = tmp_path / "config"
        cache_dir = tmp_path / "cache"
        r = _run(
            "bake", "create", "Bad-Name",
            "--mcp-stdio", f"{sys.executable} {MCP_SERVER}",
            config_dir=cfg_dir, cache_dir=cache_dir,
        )
        assert r.returncode != 0
        assert "invalid name" in r.stderr

    def test_no_source_rejected(self, tmp_path):
        cfg_dir = tmp_path / "config"
        cache_dir = tmp_path / "cache"
        r = _run(
            "bake", "create", "nosrc",
            config_dir=cfg_dir, cache_dir=cache_dir,
        )
        assert r.returncode != 0


class TestBakeOpenAPI:
    def test_openapi_with_methods_filter(self, tmp_path, petstore_server):
        cfg_dir = tmp_path / "config"
        cache_dir = tmp_path / "cache"
        r = _run(
            "bake", "create", "pets",
            "--spec", f"{petstore_server}/openapi.json",
            "--methods", "GET",
            config_dir=cfg_dir, cache_dir=cache_dir,
        )
        assert r.returncode == 0

        r = _run("@pets", "--list", config_dir=cfg_dir, cache_dir=cache_dir)
        assert r.returncode == 0
        assert "list-pets" in r.stdout
        assert "get-pet" in r.stdout
        # POST/DELETE/PUT should be filtered out
        assert "create-pet" not in r.stdout
        assert "delete-pet" not in r.stdout
        assert "update-pet" not in r.stdout


class TestBakeInstall:
    def test_install_creates_wrapper(self, tmp_path):
        cfg_dir = tmp_path / "config"
        cache_dir = tmp_path / "cache"
        _run(
            "bake", "create", "inst-test",
            "--mcp-stdio", f"{sys.executable} {MCP_SERVER}",
            config_dir=cfg_dir, cache_dir=cache_dir,
        )
        r = _run(
            "bake", "install", "inst-test",
            config_dir=cfg_dir, cache_dir=cache_dir,
        )
        assert r.returncode == 0
        assert "Installed wrapper" in r.stdout

        # Verify the wrapper file exists
        wrapper = Path.home() / ".local" / "bin" / "inst-test"
        assert wrapper.exists()
        content = wrapper.read_text()
        assert "@inst-test" in content
        # Clean up
        wrapper.unlink()

    def test_install_custom_dir(self, tmp_path):
        cfg_dir = tmp_path / "config"
        cache_dir = tmp_path / "cache"
        custom_dir = tmp_path / "custom_bin"
        _run(
            "bake", "create", "dir-test",
            "--mcp-stdio", f"{sys.executable} {MCP_SERVER}",
            config_dir=cfg_dir, cache_dir=cache_dir,
        )
        r = _run(
            "bake", "install", "dir-test", "--dir", str(custom_dir),
            config_dir=cfg_dir, cache_dir=cache_dir,
        )
        assert r.returncode == 0
        assert "Installed wrapper" in r.stdout
        wrapper = custom_dir / "dir-test"
        assert wrapper.exists()
        content = wrapper.read_text()
        assert "@dir-test" in content
        assert wrapper.stat().st_mode & 0o755

    def test_install_custom_dir_no_path_warning(self, tmp_path):
        cfg_dir = tmp_path / "config"
        cache_dir = tmp_path / "cache"
        custom_dir = tmp_path / "scripts"
        _run(
            "bake", "create", "warn-test",
            "--mcp-stdio", f"{sys.executable} {MCP_SERVER}",
            config_dir=cfg_dir, cache_dir=cache_dir,
        )
        r = _run(
            "bake", "install", "warn-test", "--dir", str(custom_dir),
            config_dir=cfg_dir, cache_dir=cache_dir,
        )
        assert r.returncode == 0
        assert "may not be in your PATH" not in r.stdout


class TestBakeShowMasking:
    """`bake show` output must be safe to paste, without changing what runs."""

    def _create(self, cfg_dir, cache_dir, name, *extra):
        r = _run(
            "bake", "create", name,
            "--spec", "https://api.example.com/openapi.json",
            *extra,
            config_dir=cfg_dir, cache_dir=cache_dir,
        )
        assert r.returncode == 0, r.stderr

    def _show(self, cfg_dir, cache_dir, name):
        r = _run("bake", "show", name, config_dir=cfg_dir, cache_dir=cache_dir)
        assert r.returncode == 0, r.stderr
        return r, json.loads(r.stdout)

    @staticmethod
    def _assert_redacted(shown, secret, output):
        # The secret must never reach a terminal, and whatever hint is left
        # behind may be no more than a short prefix of it.
        assert secret not in output
        visible = shown.rstrip("*")
        assert len(visible) <= 4, shown
        assert secret.startswith(visible), shown

    def test_oauth_client_secret_is_redacted(self, tmp_path):
        cfg_dir, cache_dir = tmp_path / "config", tmp_path / "cache"
        secret = "sk-super-secret-value"
        self._create(
            cfg_dir, cache_dir, "mask-lit",
            "--oauth-client-id", "my-client-id",
            "--oauth-client-secret", secret,
        )
        r, cfg = self._show(cfg_dir, cache_dir, "mask-lit")
        self._assert_redacted(cfg["oauth_client_secret"], secret, r.stdout + r.stderr)
        # The client id is not a secret and stays diagnosable.
        assert cfg["oauth_client_id"] == "my-client-id"

    def test_secret_shorter_than_the_hint_is_not_disclosed(self, tmp_path):
        cfg_dir, cache_dir = tmp_path / "config", tmp_path / "cache"
        self._create(
            cfg_dir, cache_dir, "mask-short",
            "--oauth-client-id", "my-client-id",
            "--oauth-client-secret", "abcd",
        )
        r, cfg = self._show(cfg_dir, cache_dir, "mask-short")
        assert "abcd" not in r.stdout + r.stderr
        assert cfg["oauth_client_secret"].strip("*") == ""

    def test_auth_header_value_is_redacted(self, tmp_path):
        cfg_dir, cache_dir = tmp_path / "config", tmp_path / "cache"
        token = "Bearer tok-abcdef123456"
        self._create(cfg_dir, cache_dir, "mask-hdr", "--auth-header", f"Authorization:{token}")
        r, cfg = self._show(cfg_dir, cache_dir, "mask-hdr")
        (header_name, shown), = cfg["auth_headers"]
        # Header names are not secrets — you need them to diagnose auth.
        assert header_name == "Authorization"
        self._assert_redacted(shown, token, r.stdout + r.stderr)

    @pytest.mark.parametrize("ref", ["env:OAUTH_SECRET", "file:/run/secrets/oauth"])
    def test_indirect_references_stay_readable(self, tmp_path, ref):
        cfg_dir, cache_dir = tmp_path / "config", tmp_path / "cache"
        self._create(
            cfg_dir, cache_dir, "mask-ref",
            "--oauth-client-secret", ref,
            "--auth-header", f"X-Key:{ref}",
        )
        _, cfg = self._show(cfg_dir, cache_dir, "mask-ref")
        assert cfg["oauth_client_secret"] == ref
        assert cfg["auth_headers"] == [["X-Key", ref]]

    def test_masking_does_not_change_what_the_baked_tool_sends(self, tmp_path, monkeypatch):
        import mcp2cli

        cfg_dir, cache_dir = tmp_path / "config", tmp_path / "cache"
        secret, token = "sk-super-secret-value", "Bearer tok-abcdef123456"
        self._create(
            cfg_dir, cache_dir, "mask-run",
            "--oauth-client-id", "my-client-id",
            "--oauth-client-secret", secret,
            "--auth-header", f"Authorization:{token}",
        )
        self._show(cfg_dir, cache_dir, "mask-run")

        # What `mcp2cli @mask-run ...` would run with: the real credentials.
        monkeypatch.setattr(mcp2cli, "BAKED_FILE", cfg_dir / "baked.json")
        argv = _baked_to_argv(_load_baked("mask-run"))
        assert secret in argv
        assert f"Authorization:{token}" in argv
