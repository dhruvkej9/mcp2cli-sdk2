"""Robustness against real-world MCP server behaviour.

Each test here corresponds to a shape that a published MCP server actually
emits and that used to break mcp2cli — see plan.md. Everything is offline: the
hostile server is a raw JSON-RPC process, so no network and no MCP SDK server
normalisation stands between the test and the wire format.
"""

import json
import subprocess
import sys
from pathlib import Path

import pytest

import mcp2cli

HOSTILE = str(Path(__file__).parent / "hostile_server.py")


def run_cli(*args, mode="all", stdin_data=None, timeout=60, env=None):
    cmd = [
        sys.executable,
        "-m",
        "mcp2cli",
        "--mcp-stdio",
        f"{sys.executable} {HOSTILE} {mode}",
        "--refresh",
        *args,
    ]
    return subprocess.run(
        cmd, capture_output=True, text=True, input=stdin_data, timeout=timeout, env=env
    )


def assert_no_traceback(result):
    """No failure mode should ever reach the user as a Python traceback."""
    assert "Traceback (most recent call last)" not in result.stderr
    assert "ExceptionGroup" not in result.stderr


def sent_args(result):
    """Arguments the hostile server received, for echo-style tools."""
    return json.loads(result.stdout)["args"]


class TestPagination:
    """tools/resources/prompts lists must follow nextCursor to the end."""

    def test_list_includes_second_page_tools(self):
        r = run_cli("--list")
        assert r.returncode == 0
        assert "get-user" in r.stdout  # page 1
        assert "page-two-tool" in r.stdout  # page 2

    def test_second_page_tool_is_callable(self):
        r = run_cli("page-two-tool")
        assert r.returncode == 0, r.stderr
        assert json.loads(r.stdout)["tool"] == "page_two_tool"

    def test_list_json_has_all_pages(self):
        r = run_cli("--list", "--json")
        assert r.returncode == 0
        names = [c["name"] for c in json.loads(r.stdout)]
        assert "get-user" in names and "page-two-tool" in names

    def test_resources_paginate(self):
        r = run_cli("--list-resources")
        assert r.returncode == 0, r.stderr
        uris = [item["uri"] for item in json.loads(r.stdout)]
        assert "hostile://doc/one" in uris and "hostile://doc/two" in uris

    def test_resource_templates_paginate(self):
        r = run_cli("--list-resource-templates")
        assert r.returncode == 0, r.stderr
        templates = [t["uriTemplate"] for t in json.loads(r.stdout)]
        assert "hostile://doc/{name}" in templates
        assert "hostile://blob/{name}" in templates

    def test_prompts_paginate(self):
        r = run_cli("--list-prompts")
        assert r.returncode == 0, r.stderr
        names = [p["name"] for p in json.loads(r.stdout)]
        assert "first_prompt" in names and "second_prompt" in names

    def test_repeated_cursor_does_not_loop_forever(self):
        """A server that keeps returning the same cursor must not hang us."""

        class Result:
            tools = [type("T", (), {"name": "x", "description": "", "input_schema": {}})()]
            next_cursor = "same"

        calls = []

        async def list_fn(params=None):
            calls.append(params)
            return Result()

        import anyio

        items = anyio.run(lambda: mcp2cli._paginate(list_fn, "tools"))
        assert len(calls) == 2  # first page, then the repeat is detected
        assert len(items) == 2


class TestNameCollisions:
    """Distinct tool names that kebab-case to the same CLI name."""

    def test_list_shows_both_colliding_tools(self):
        r = run_cli("--list", "--compact")
        assert r.returncode == 0, r.stderr
        names = r.stdout.split()
        assert "get-user" in names
        assert "get-user-2" in names

    def test_first_collision_maps_to_original_wire_name(self):
        r = run_cli("get-user", "--id", "42")
        assert r.returncode == 0, r.stderr
        assert json.loads(r.stdout)["tool"] == "get_user"

    def test_second_collision_maps_to_its_own_wire_name(self):
        r = run_cli("get-user-2", "--id", "42")
        assert r.returncode == 0, r.stderr
        assert json.loads(r.stdout)["tool"] == "getUser"

    def test_help_still_works(self):
        """A collision used to break --help for the whole server."""
        r = run_cli("--list")
        assert r.returncode == 0
        assert_no_traceback(r)


class TestReservedFlagNames:
    """Tool properties named help / stdin / json."""

    def test_tool_help_does_not_crash(self):
        r = run_cli("reserved-flags", "--help")
        assert r.returncode == 0, r.stderr
        assert_no_traceback(r)

    def test_renamed_flags_reach_the_server_under_their_real_names(self):
        r = run_cli(
            "reserved-flags",
            "--arg-help",
            "H",
            "--arg-stdin",
            "S",
            "--arg-json",
            "J",
        )
        assert r.returncode == 0, r.stderr
        assert sent_args(r) == {"help": "H", "stdin": "S", "json": "J"}

    def test_other_tools_unaffected(self):
        """One hostile tool must not disable the rest of the server."""
        r = run_cli("get-user", "--id", "1")
        assert r.returncode == 0, r.stderr


class TestSchemaShapes:
    def test_int_enum_accepts_its_values(self):
        r = run_cli("int-enum", "--level", "2")
        assert r.returncode == 0, r.stderr
        assert sent_args(r) == {"level": 2}

    def test_int_enum_still_rejects_bad_values(self):
        r = run_cli("int-enum", "--level", "9")
        assert r.returncode != 0
        assert "invalid choice" in r.stderr

    def test_union_type_list_sends_a_number(self):
        r = run_cli("union-type", "--count", "5")
        assert r.returncode == 0, r.stderr
        assert sent_args(r) == {"count": 5}

    def test_anyof_number_sends_a_float(self):
        r = run_cli("union-type", "--ratio", "1.5")
        assert r.returncode == 0, r.stderr
        assert sent_args(r) == {"ratio": 1.5}

    def test_anyof_boolean_is_a_flag(self):
        r = run_cli("union-type", "--flag")
        assert r.returncode == 0, r.stderr
        assert sent_args(r) == {"flag": True}

    def test_boolean_can_be_sent_false(self):
        r = run_cli("toggle", "--no-enabled")
        assert r.returncode == 0, r.stderr
        assert sent_args(r) == {"enabled": False}

    def test_boolean_can_be_sent_true(self):
        r = run_cli("toggle", "--enabled")
        assert r.returncode == 0, r.stderr
        assert sent_args(r) == {"enabled": True}

    def test_boolean_omitted_is_omitted(self):
        r = run_cli("toggle")
        assert r.returncode == 0, r.stderr
        assert sent_args(r) == {}

    def test_schemaless_tool_takes_json(self):
        r = run_cli("no-schema", "--json", '{"a": 1}')
        assert r.returncode == 0, r.stderr
        assert sent_args(r) == {"a": 1}

    def test_schemaless_tool_takes_arg_pairs(self):
        r = run_cli("no-schema", "--arg", "a=1", "--arg", "b=two")
        assert r.returncode == 0, r.stderr
        assert sent_args(r) == {"a": "1", "b": "two"}


class TestResultRendering:
    def test_structured_content_only_is_printed(self):
        r = run_cli("structured-only")
        assert r.returncode == 0, r.stderr
        assert json.loads(r.stdout) == {"ok": True, "count": 42}

    def test_structured_content_in_json_envelope(self):
        r = run_cli("--json", "structured-only")
        assert r.returncode == 0, r.stderr
        assert json.loads(r.stdout)["structuredContent"] == {"ok": True, "count": 42}

    def test_resource_link_and_embedded_resource_are_rendered(self):
        r = run_cli("links")
        assert r.returncode == 0, r.stderr
        assert "see links" in r.stdout
        assert "hostile://doc/one" in r.stdout  # resource_link
        assert "embedded body" in r.stdout  # embedded resource

    def test_head_truncates_text_lines(self):
        r = run_cli("--head", "1", "links")
        assert r.returncode == 0, r.stderr
        assert r.stdout.strip() == "see links"

    def test_resource_read_returns_text(self):
        r = run_cli("--read-resource", "hostile://doc/one")
        assert r.returncode == 0, r.stderr
        assert "page one body" in r.stdout

    def test_resource_read_returns_blob(self):
        r = run_cli("--read-resource", "hostile://blob/data")
        assert r.returncode == 0, r.stderr
        assert "aGVsbG8=" in r.stdout

    def test_prompt_with_embedded_resource_message(self):
        r = run_cli("--get-prompt", "second_prompt")
        assert r.returncode == 0, r.stderr
        assert "resource-backed prompt" in r.stdout


class TestErrorSurfaces:
    """Every failure is one clean line on stderr with a non-zero exit."""

    def test_is_error_result(self):
        r = run_cli("boom")
        assert r.returncode == 1
        assert "tool exploded" in r.stderr
        assert_no_traceback(r)

    def test_jsonrpc_error_response(self):
        r = run_cli("rpc_error".replace("_", "-"))
        assert r.returncode == 1
        assert "internal server meltdown" in r.stderr
        assert_no_traceback(r)

    def test_unknown_resource(self):
        r = run_cli("--read-resource", "hostile://nope")
        assert r.returncode == 1
        assert "not found" in r.stderr.lower()
        assert_no_traceback(r)

    def test_unknown_prompt(self):
        r = run_cli("--get-prompt", "nope")
        assert r.returncode == 1
        assert "not found" in r.stderr.lower()
        assert_no_traceback(r)

    def test_server_that_dies_at_startup(self):
        r = run_cli("--list", mode="crash")
        assert r.returncode == 1
        assert_no_traceback(r)
        assert "Error:" in r.stderr

    def test_unreachable_http_server(self):
        r = subprocess.run(
            [
                sys.executable, "-m", "mcp2cli",
                "--mcp", "http://127.0.0.1:9/mcp",
                "--refresh", "--list",
            ],
            capture_output=True, text=True, timeout=60,
        )
        assert r.returncode == 1
        assert_no_traceback(r)
        assert "Error:" in r.stderr

    def test_debug_env_restores_traceback(self):
        import os

        env = {**os.environ, "MCP2CLI_DEBUG": "1"}
        r = run_cli("--list", mode="crash", env=env)
        assert r.returncode != 0
        assert "Traceback" in r.stderr

    @pytest.mark.slow
    def test_timeout_is_enforced(self):
        r = run_cli("--timeout", "1", "slow", "--seconds", "10", timeout=60)
        assert r.returncode == 1
        assert_no_traceback(r)

    def test_empty_tool_list_is_not_an_error(self):
        r = run_cli("--list", mode="empty")
        assert r.returncode == 0, r.stderr
        assert_no_traceback(r)

    def test_server_that_prints_a_banner_on_stdout(self):
        """Non-JSON noise before the handshake must not break the session."""
        r = run_cli("--list", "--compact", mode="noisy")
        assert r.returncode == 0, r.stderr
        assert "get-user" in r.stdout

    def test_banner_noise_keeps_stdout_parseable(self):
        r = run_cli("--list", "--json", mode="noisy")
        assert r.returncode == 0, r.stderr
        assert isinstance(json.loads(r.stdout), list)


class TestHostileNames:
    """Server-side names that are not valid CLI words."""

    def test_slash_in_tool_name(self):
        r = run_cli("browser-navigate", "--url", "https://x.dev")
        assert r.returncode == 0, r.stderr
        assert json.loads(r.stdout)["tool"] == "browser/navigate"

    def test_dotted_capitalised_tool_name(self):
        r = run_cli("--list", "--compact")
        assert r.returncode == 0, r.stderr
        assert "slack.post-message" in r.stdout

    def test_colliding_property_names_both_reach_the_server(self):
        """fooBar and foo_bar both kebab to --foo-bar; neither may be lost."""
        r = run_cli("dup-params", "--foo-bar", "camel", "--foo-bar-2", "snake")
        assert r.returncode == 0, r.stderr
        assert sent_args(r) == {"fooBar": "camel", "foo_bar": "snake"}

    def test_to_cli_name(self):
        assert mcp2cli.to_cli_name("browser/navigate") == "browser-navigate"
        assert mcp2cli.to_cli_name("Slack.postMessage") == "slack.post-message"
        assert mcp2cli.to_cli_name("search files") == "search-files"
        assert mcp2cli.to_cli_name("--weird--") == "weird"
        assert mcp2cli.to_cli_name("") == "tool"


class TestUniversalEscapeHatch:
    """Every MCP tool accepts raw JSON, not just schema-less ones."""

    def test_json_payload_on_a_schema_bearing_tool(self):
        r = run_cli("get-user", "--json", '{"id": "raw"}')
        assert r.returncode == 0, r.stderr
        assert sent_args(r) == {"id": "raw"}

    def test_arg_pairs_on_a_schema_bearing_tool(self):
        r = run_cli("get-user", "--arg", "id=raw", "--arg", "extra=1")
        assert r.returncode == 0, r.stderr
        assert sent_args(r) == {"id": "raw", "extra": "1"}

    def test_stdin_on_a_schema_bearing_tool(self):
        r = run_cli("get-user", "--stdin", stdin_data='{"id": "piped"}')
        assert r.returncode == 0, r.stderr
        assert sent_args(r) == {"id": "piped"}

    def test_sources_are_mutually_exclusive(self):
        r = run_cli("get-user", "--json", "{}", "--arg", "id=1")
        assert r.returncode != 0
        assert "mutually exclusive" in r.stderr


class TestMalformedPayloads:
    """One out-of-spec field must not make a whole server unusable."""

    def test_tool_list_survives_a_null_input_schema(self):
        r = run_cli("--list", "--compact", mode="malformed")
        assert r.returncode == 0, r.stderr
        names = r.stdout.split()
        assert "broken-schema" in names  # the offender itself
        assert "get-user" in names  # and everything around it

    def test_lenient_read_is_announced(self):
        r = run_cli("--list", mode="malformed")
        assert r.returncode == 0, r.stderr
        assert "lenient" in r.stderr

    def test_well_formed_server_uses_the_strict_path(self):
        """The fallback must not mask a server that is already compliant."""
        r = run_cli("--list", mode="all")
        assert r.returncode == 0, r.stderr
        assert "lenient" not in r.stderr

    def test_empty_object_schema_is_callable(self):
        """`inputSchema: {}` is what CLI-wrapper servers publish."""
        r = run_cli("empty-schema", "--json", '{"a": 1}', mode="malformed")
        assert r.returncode == 0, r.stderr
        assert sent_args(r) == {"a": 1}

    def test_tool_with_null_schema_is_callable(self):
        r = run_cli("broken-schema", "--json", '{"x": 1}', mode="malformed")
        assert r.returncode == 0, r.stderr
        assert sent_args(r) == {"x": 1}

    def test_other_tools_still_callable(self):
        r = run_cli("get-user", "--id", "7", mode="malformed")
        assert r.returncode == 0, r.stderr
        assert sent_args(r) == {"id": "7"}

    def test_unknown_content_block_still_yields_the_text(self):
        r = run_cli("bad-content", mode="malformed")
        assert r.returncode == 0, r.stderr
        assert "salvaged text" in r.stdout

    def test_resource_list_survives_a_nameless_resource(self):
        r = run_cli("--list-resources", mode="malformed")
        assert r.returncode == 0, r.stderr
        uris = [item["uri"] for item in json.loads(r.stdout)]
        assert "hostile://doc/three" in uris
        assert "hostile://doc/one" in uris

    def test_json_output_still_valid_on_the_lenient_path(self):
        r = run_cli("--json", "--list", mode="malformed")
        assert r.returncode == 0, r.stderr
        assert isinstance(json.loads(r.stdout), list)


class TestClientIdentity:
    def test_server_sees_mcp2cli_client_info(self):
        r = run_cli("whoami")
        assert r.returncode == 0, r.stderr
        info = json.loads(r.stdout)
        assert info.get("name") == "mcp2cli"
        assert info.get("version")

    def test_client_info_helper(self):
        info = mcp2cli._mcp_client_info()
        assert info is not None and info.name == "mcp2cli"


class TestSchemaHelpers:
    """Unit coverage for the schema resolution the CLI builds flags from."""

    @pytest.mark.parametrize(
        "schema,expected",
        [
            ({"type": "integer"}, "integer"),
            ({"type": ["integer", "null"]}, "integer"),
            ({"type": ["null", "number"]}, "number"),
            ({"anyOf": [{"type": "boolean"}, {"type": "null"}]}, "boolean"),
            ({"oneOf": [{"type": "null"}, {"type": "string"}]}, "string"),
            ({"enum": [1, 2, 3]}, "integer"),
            ({"enum": [1.5, 2]}, "number"),
            ({"enum": ["a", "b"]}, "string"),
            ({"enum": [1, "a"]}, None),
            ({}, None),
            ({"type": "bogus"}, None),
            (None, None),
        ],
    )
    def test_effective_schema_type(self, schema, expected):
        assert mcp2cli.effective_schema_type(schema) == expected

    @pytest.mark.parametrize(
        "value,schema,expected",
        [
            ("5", {"type": ["integer", "null"]}, 5),
            ("1.5", {"anyOf": [{"type": "number"}]}, 1.5),
            ("false", {"type": "boolean"}, False),
            ("true", {"type": "boolean"}, True),
            ("notanumber", {"type": "integer"}, "notanumber"),
            ("a,b", {"type": "array", "items": {"type": ["string", "null"]}}, ["a", "b"]),
            ("1,2", {"type": "array", "items": {"type": "integer"}}, [1, 2]),
            ('{"k": 1}', {"type": "object"}, {"k": 1}),
        ],
    )
    def test_coerce_value(self, value, schema, expected):
        assert mcp2cli.coerce_value(value, schema) == expected

    def test_safe_flag_name(self):
        assert mcp2cli.safe_flag_name("help") == "arg-help"
        assert mcp2cli.safe_flag_name("stdin") == "arg-stdin"
        assert mcp2cli.safe_flag_name("path") == "path"

    def test_dedupe_command_names(self):
        cmds = [
            mcp2cli.CommandDef(name="a"),
            mcp2cli.CommandDef(name="a"),
            mcp2cli.CommandDef(name="a"),
            mcp2cli.CommandDef(name="b"),
        ]
        assert [c.name for c in mcp2cli.dedupe_command_names(cmds)] == [
            "a", "a-2", "a-3", "b",
        ]

    def test_extract_mcp_commands_tolerates_junk_schemas(self):
        tools = [
            {"name": "a", "inputSchema": None},
            {"name": "b", "inputSchema": {"properties": "not-a-dict"}},
            {"name": "c", "inputSchema": {"properties": {"x": "not-a-dict"}}},
            {"name": "d"},
        ]
        commands = mcp2cli.extract_mcp_commands(tools)
        assert [c.name for c in commands] == ["a", "b", "c", "d"]

    def test_argparse_choices_are_coerced_to_flag_type(self):
        p = mcp2cli.ParamDef(
            name="level", original_name="level", python_type=int, choices=[1, 2]
        )
        assert mcp2cli._argparse_choices(p) == [1, 2]

    def test_argparse_choices_dropped_when_unrepresentable(self):
        p = mcp2cli.ParamDef(
            name="mixed", original_name="mixed", python_type=int, choices=["a", 1]
        )
        assert mcp2cli._argparse_choices(p) is None


class TestContentRendering:
    """Unit coverage for content-block rendering."""

    class _Block:
        def __init__(self, **kw):
            for k, v in kw.items():
                setattr(self, k, v)

    def test_text_block(self):
        assert mcp2cli._render_block(self._Block(text="hi")) == "hi"

    def test_image_block(self):
        assert mcp2cli._render_block(self._Block(data="b64")) == "b64"

    def test_resource_link_block(self):
        block = self._Block(uri="file:///x", name="x")
        assert mcp2cli._render_block(block) == "x: file:///x"

    def test_embedded_resource_text(self):
        block = self._Block(resource=self._Block(text="body", uri="file:///x"))
        assert mcp2cli._render_block(block) == "body"

    def test_embedded_resource_blob(self):
        block = self._Block(resource=self._Block(blob="b64", uri="file:///x"))
        assert mcp2cli._render_block(block) == "b64"

    def test_unknown_block_is_skipped(self):
        assert mcp2cli._render_block(self._Block(kind="mystery")) is None
        assert mcp2cli._extract_content_parts([self._Block(kind="mystery")]) == ""

    def test_render_tool_result_prefers_content(self):
        result = self._Block(
            content=[self._Block(text="hello")], structured_content={"a": 1}
        )
        assert mcp2cli.render_tool_result(result) == "hello"

    def test_render_tool_result_falls_back_to_structured(self):
        result = self._Block(content=[], structured_content={"a": 1})
        assert mcp2cli.render_tool_result(result) == '{"a": 1}'

    def test_render_tool_result_empty(self):
        result = self._Block(content=[], structured_content=None)
        assert mcp2cli.render_tool_result(result) == ""

    def test_head_text(self):
        assert mcp2cli._head_text("a\nb\nc", 2) == "a\nb"
        assert mcp2cli._head_text("a\nb", None) == "a\nb"
