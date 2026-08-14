"""A hostile-but-protocol-legal MCP server, spoken in raw JSON-RPC.

Every shape here is something a real MCP server in the wild does, and every one
of them used to break mcp2cli:

* paginated ``tools/list`` / ``resources/list`` / ``prompts/list`` (``nextCursor``)
* two distinct tool names that kebab-case to the *same* CLI name
* tool properties named ``help`` / ``stdin`` / ``json`` (argparse owns those)
* an ``enum`` of ints with no declared ``type``
* union types (``{"type": ["integer", "null"]}``, ``anyOf``)
* a result with ``content: []`` and only ``structuredContent``
* ``resource_link`` and embedded ``resource`` content blocks
* ``isError`` results and JSON-RPC error responses

It deliberately does *not* use the MCP SDK: the SDK would normalise away the
very shapes under test.

Usage: ``python hostile_server.py [mode]`` where mode is one of
``all`` (default), ``crash`` (exit before the handshake), ``onepage``
(no pagination), ``empty`` (no tools at all).
"""

import json
import sys
import time

PROTOCOL_FALLBACK = "2025-06-18"

PAGE_ONE = [
    {
        "name": "get_user",
        "description": "snake_case name",
        "inputSchema": {
            "type": "object",
            "properties": {"id": {"type": "string", "description": "user id"}},
        },
    },
    {
        "name": "getUser",
        "description": "camelCase name that kebabs to the same CLI name",
        "inputSchema": {
            "type": "object",
            "properties": {"id": {"type": "string", "description": "user id"}},
        },
    },
    {
        "name": "reserved_flags",
        "description": "properties named like argparse's own options",
        "inputSchema": {
            "type": "object",
            "properties": {
                "help": {"type": "string", "description": "not argparse's help"},
                "stdin": {"type": "string", "description": "not mcp2cli's stdin"},
                "json": {"type": "string", "description": "not mcp2cli's json"},
            },
        },
    },
    {
        "name": "int_enum",
        "description": "enum of ints, no declared type",
        "inputSchema": {
            "type": "object",
            "properties": {"level": {"enum": [1, 2, 3], "description": "level"}},
            "required": ["level"],
        },
    },
    {
        "name": "union_type",
        "description": "union type list and anyOf",
        "inputSchema": {
            "type": "object",
            "properties": {
                "count": {"type": ["integer", "null"], "description": "how many"},
                "ratio": {"anyOf": [{"type": "number"}, {"type": "null"}]},
                "flag": {"anyOf": [{"type": "boolean"}, {"type": "null"}]},
            },
        },
    },
    {
        "name": "toggle",
        "description": "a boolean that defaults to true, so false must be sendable",
        "inputSchema": {
            "type": "object",
            "properties": {"enabled": {"type": "boolean", "default": True}},
        },
    },
]

PAGE_TWO = [
    {
        "name": "page_two_tool",
        "description": "only reachable by following nextCursor",
        "inputSchema": {"type": "object", "properties": {}},
    },
    {
        "name": "structured_only",
        "description": "empty content, structuredContent only",
        "inputSchema": {"type": "object", "properties": {}},
    },
    {
        "name": "links",
        "description": "resource_link plus embedded resource blocks",
        "inputSchema": {"type": "object", "properties": {}},
    },
    {
        "name": "boom",
        "description": "returns isError",
        "inputSchema": {"type": "object", "properties": {}},
    },
    {
        "name": "rpc_error",
        "description": "answers with a JSON-RPC error",
        "inputSchema": {"type": "object", "properties": {}},
    },
    {
        "name": "slow",
        "description": "sleeps, to exercise --timeout",
        "inputSchema": {
            "type": "object",
            "properties": {"seconds": {"type": "number", "default": 5}},
        },
    },
    {
        "name": "no_schema",
        "description": "declares no properties (CLI-wrapper style)",
        "inputSchema": {"type": "object"},
    },
    {
        "name": "whoami",
        "description": "echoes back the clientInfo seen during initialize",
        "inputSchema": {"type": "object", "properties": {}},
    },
    {
        "name": "browser/navigate",
        "description": "a name containing a slash",
        "inputSchema": {"type": "object", "properties": {"url": {"type": "string"}}},
    },
    {
        "name": "Slack.postMessage",
        "description": "a name with a dot and capitals",
        "inputSchema": {"type": "object", "properties": {"text": {"type": "string"}}},
    },
    {
        "name": "roots_probe",
        "description": "asks the client for its roots and reports them back",
        "inputSchema": {"type": "object", "properties": {}},
    },
    {
        "name": "sampling_probe",
        "description": "asks the client to sample an LLM; expects a refusal",
        "inputSchema": {"type": "object", "properties": {}},
    },
    {
        "name": "elicit_probe",
        "description": "asks the client to elicit input; expects a refusal",
        "inputSchema": {"type": "object", "properties": {}},
    },
    {
        "name": "progress_probe",
        "description": "emits progress notifications, then finishes",
        "inputSchema": {"type": "object", "properties": {}},
    },
    {
        "name": "dup_params",
        "description": "two properties that kebab to the same flag",
        "inputSchema": {
            "type": "object",
            "properties": {
                "fooBar": {"type": "string", "description": "camel"},
                "foo_bar": {"type": "string", "description": "snake"},
            },
        },
    },
]

# Only served in --mode malformed: payloads the MCP schema rejects outright.
# The SDK refuses the whole response for any one of these, so mcp2cli has to
# fall back to an unvalidated read or the server is unusable.
MALFORMED_TOOLS = [
    {
        # `{}` is what plenty of CLI-wrapper servers publish, and the SDK's
        # result validation rejects it for want of a "type".
        "name": "empty_schema",
        "description": "inputSchema is {} — rejected by the SDK's result schema",
        "inputSchema": {},
    },
    {
        "name": "broken_schema",
        "description": "inputSchema is null — the SDK rejects the entire list",
        "inputSchema": None,
    },
    {
        "name": "bad_content",
        "description": "returns an unknown content block type",
        "inputSchema": {"type": "object", "properties": {}},
    },
]

MALFORMED_RESOURCES = [
    # No "name": required by the schema, so the whole list fails to validate.
    {"uri": "hostile://doc/three", "description": "nameless resource"}
]

# clientInfo recorded from the initialize request, echoed by the whoami tool.
SEEN_CLIENT_INFO: dict = {}

RESOURCES_PAGE_ONE = [
    {
        "uri": "hostile://doc/one",
        "name": "one",
        "description": "first page resource",
        "mimeType": "text/plain",
    }
]
RESOURCES_PAGE_TWO = [
    {
        "uri": "hostile://doc/two",
        "name": "two",
        "description": "second page resource",
        "mimeType": "text/plain",
    }
]

PROMPTS_PAGE_ONE = [
    {
        "name": "first_prompt",
        "description": "page one prompt",
        "arguments": [{"name": "topic", "description": "a topic", "required": True}],
    }
]
PROMPTS_PAGE_TWO = [
    {"name": "second_prompt", "description": "page two prompt", "arguments": []}
]

TEMPLATES_PAGE_ONE = [
    {
        "uriTemplate": "hostile://doc/{name}",
        "name": "doc",
        "description": "page one template",
        "mimeType": "text/plain",
    }
]
TEMPLATES_PAGE_TWO = [
    {
        "uriTemplate": "hostile://blob/{name}",
        "name": "blob",
        "description": "page two template",
        "mimeType": "application/octet-stream",
    }
]


def _send(msg: dict) -> None:
    sys.stdout.write(json.dumps(msg) + "\n")
    sys.stdout.flush()


_NEXT_REQUEST_ID = [9000]


def _ask_client(method: str, params: dict) -> dict:
    """Send a server->client request and block for its response.

    Messages that arrive while waiting (notifications, other traffic) are
    dropped: this is a test fixture, not a conforming implementation.
    """
    _NEXT_REQUEST_ID[0] += 1
    req_id = _NEXT_REQUEST_ID[0]
    _send({"jsonrpc": "2.0", "id": req_id, "method": method, "params": params})
    while True:
        line = sys.stdin.readline()
        if not line:
            return {"error": {"message": "client closed the connection"}}
        try:
            msg = json.loads(line)
        except json.JSONDecodeError:
            continue
        if msg.get("id") == req_id:
            return msg


def _respond(msg_id, result=None, error=None) -> None:
    if msg_id is None:
        return
    resp = {"jsonrpc": "2.0", "id": msg_id}
    if error is not None:
        resp["error"] = error
    else:
        resp["result"] = result
    _send(resp)


def _call_tool(name: str, arguments: dict, meta: dict | None = None):
    """Return (result, error) for a tools/call."""
    if name == "roots_probe":
        reply = _ask_client("roots/list", {})
        roots = (reply.get("result") or {}).get("roots", [])
        return {"content": [{"type": "text", "text": json.dumps(roots)}]}, None
    if name in ("sampling_probe", "elicit_probe"):
        method = (
            "sampling/createMessage"
            if name == "sampling_probe"
            else "elicitation/create"
        )
        params = (
            {"messages": [], "maxTokens": 10}
            if name == "sampling_probe"
            else {"message": "need input", "requestedSchema": {"type": "object"}}
        )
        reply = _ask_client(method, params)
        payload = {
            "refused": "error" in reply,
            "message": (reply.get("error") or {}).get("message", ""),
        }
        return {"content": [{"type": "text", "text": json.dumps(payload)}]}, None
    if name == "progress_probe":
        token = (meta or {}).get("progressToken")
        if token is not None:
            for step in (1, 2, 3):
                _send({
                    "jsonrpc": "2.0",
                    "method": "notifications/progress",
                    "params": {
                        "progressToken": token,
                        "progress": step,
                        "total": 3,
                        "message": f"step {step}",
                    },
                })
        return {"content": [{"type": "text", "text": "progress done"}]}, None
    if name == "whoami":
        return {
            "content": [{"type": "text", "text": json.dumps(SEEN_CLIENT_INFO)}]
        }, None
    if name == "bad_content":
        # An unknown block type makes the SDK reject the whole result; the
        # text block alongside it is still perfectly usable.
        return {
            "content": [
                {"type": "quantum", "payload": "unreadable"},
                {"type": "text", "text": "salvaged text"},
            ]
        }, None
    if name == "structured_only":
        return {"content": [], "structuredContent": {"ok": True, "count": 42}}, None
    if name == "links":
        return {
            "content": [
                {"type": "text", "text": "see links"},
                {
                    "type": "resource_link",
                    "uri": "hostile://doc/one",
                    "name": "one",
                    "mimeType": "text/plain",
                },
                {
                    "type": "resource",
                    "resource": {
                        "uri": "hostile://doc/two",
                        "mimeType": "text/plain",
                        "text": "embedded body",
                    },
                },
            ]
        }, None
    if name == "boom":
        return {
            "content": [{"type": "text", "text": "tool exploded"}],
            "isError": True,
        }, None
    if name == "rpc_error":
        return None, {"code": -32603, "message": "internal server meltdown"}
    if name == "slow":
        time.sleep(float(arguments.get("seconds", 5)))
        return {"content": [{"type": "text", "text": "finally done"}]}, None
    return {
        "content": [
            {"type": "text", "text": json.dumps({"tool": name, "args": arguments})}
        ]
    }, None


def _read_resource(uri: str):
    if uri == "hostile://doc/one":
        return {
            "contents": [
                {"uri": uri, "mimeType": "text/plain", "text": "page one body"}
            ]
        }, None
    if uri == "hostile://doc/two":
        return {
            "contents": [
                {"uri": uri, "mimeType": "text/plain", "text": "page two body"}
            ]
        }, None
    if uri == "hostile://blob/data":
        return {
            "contents": [
                {
                    "uri": uri,
                    "mimeType": "application/octet-stream",
                    "blob": "aGVsbG8=",
                }
            ]
        }, None
    return None, {"code": -32602, "message": f"Resource {uri} not found"}


def _get_prompt(name: str, arguments: dict):
    if name == "first_prompt":
        return {
            "description": "page one prompt",
            "messages": [
                {
                    "role": "user",
                    "content": {
                        "type": "text",
                        "text": f"Tell me about {arguments.get('topic', '?')}",
                    },
                }
            ],
        }, None
    if name == "second_prompt":
        # A prompt whose message is an embedded resource, not plain text.
        return {
            "description": "page two prompt",
            "messages": [
                {
                    "role": "user",
                    "content": {
                        "type": "resource",
                        "resource": {
                            "uri": "hostile://doc/one",
                            "mimeType": "text/plain",
                            "text": "resource-backed prompt",
                        },
                    },
                }
            ],
        }, None
    return None, {"code": -32602, "message": f"Prompt {name} not found"}


def main() -> None:
    mode = sys.argv[1] if len(sys.argv) > 1 else "all"

    if mode == "crash":
        print("hostile server: refusing to start", file=sys.stderr)
        sys.exit(3)

    if mode == "noisy":
        # Servers that print a banner on stdout before speaking JSON-RPC.
        print("Server starting up...")
        sys.stdout.flush()

    paginate = mode not in ("onepage", "empty")
    if mode == "empty":
        page_one, page_two = [], []
    elif mode == "malformed":
        page_one, page_two = PAGE_ONE + MALFORMED_TOOLS, PAGE_TWO
    elif paginate:
        page_one, page_two = PAGE_ONE, PAGE_TWO
    else:
        page_one, page_two = PAGE_ONE + PAGE_TWO, []

    resources_page_one = RESOURCES_PAGE_ONE + (
        MALFORMED_RESOURCES if mode == "malformed" else []
    )

    while True:
        raw_line = sys.stdin.readline()
        if not raw_line:
            break
        line = raw_line.strip()
        if not line:
            continue
        try:
            msg = json.loads(line)
        except json.JSONDecodeError:
            continue

        msg_id = msg.get("id")
        method = msg.get("method", "")
        params = msg.get("params") or {}
        cursor = params.get("cursor")

        if method == "initialize":
            SEEN_CLIENT_INFO.update(params.get("clientInfo") or {})
            _respond(
                msg_id,
                {
                    "protocolVersion": params.get("protocolVersion", PROTOCOL_FALLBACK),
                    "capabilities": {
                        "tools": {},
                        "resources": {"subscribe": True},
                        "prompts": {},
                        "logging": {},
                        "completions": {},
                    },
                    "serverInfo": {"name": "hostile-server", "version": "1.0.0"},
                    # Echo what the client told us about itself so tests can
                    # assert mcp2cli identifies itself.
                    "instructions": json.dumps(params.get("clientInfo") or {}),
                },
            )
        elif method in ("notifications/initialized", "initialized"):
            continue
        elif method == "ping":
            _respond(msg_id, {})
        elif method == "tools/list":
            if cursor == "page2":
                _respond(msg_id, {"tools": page_two})
            elif paginate and page_two:
                _respond(msg_id, {"tools": page_one, "nextCursor": "page2"})
            else:
                _respond(msg_id, {"tools": page_one})
        elif method == "tools/call":
            result, error = _call_tool(
                params.get("name", ""),
                params.get("arguments") or {},
                params.get("_meta") or {},
            )
            _respond(msg_id, result, error)
        elif method == "completion/complete":
            argument = params.get("argument") or {}
            prefix = argument.get("value", "")
            pool = ["Engineering", "Marketing", "Sales", "Support"]
            values = [v for v in pool if v.lower().startswith(prefix.lower())]
            _respond(
                msg_id,
                {"completion": {"values": values, "total": len(values), "hasMore": False}},
            )
        elif method == "logging/setLevel":
            _respond(msg_id, {})
            _send({
                "jsonrpc": "2.0",
                "method": "notifications/message",
                "params": {
                    "level": params.get("level", "info"),
                    "logger": "hostile",
                    "data": "log level accepted",
                },
            })
        elif method in ("resources/subscribe", "resources/unsubscribe"):
            _respond(msg_id, {})
        elif method == "resources/list":
            if cursor == "page2":
                _respond(msg_id, {"resources": RESOURCES_PAGE_TWO})
            else:
                _respond(
                    msg_id,
                    {"resources": resources_page_one, "nextCursor": "page2"}
                    if paginate
                    else {"resources": resources_page_one + RESOURCES_PAGE_TWO},
                )
        elif method == "resources/templates/list":
            if cursor == "page2":
                _respond(msg_id, {"resourceTemplates": TEMPLATES_PAGE_TWO})
            else:
                _respond(
                    msg_id,
                    {"resourceTemplates": TEMPLATES_PAGE_ONE, "nextCursor": "page2"}
                    if paginate
                    else {
                        "resourceTemplates": TEMPLATES_PAGE_ONE + TEMPLATES_PAGE_TWO
                    },
                )
        elif method == "resources/read":
            result, error = _read_resource(params.get("uri", ""))
            _respond(msg_id, result, error)
        elif method == "prompts/list":
            if cursor == "page2":
                _respond(msg_id, {"prompts": PROMPTS_PAGE_TWO})
            else:
                _respond(
                    msg_id,
                    {"prompts": PROMPTS_PAGE_ONE, "nextCursor": "page2"}
                    if paginate
                    else {"prompts": PROMPTS_PAGE_ONE + PROMPTS_PAGE_TWO},
                )
        elif method == "prompts/get":
            result, error = _get_prompt(
                params.get("name", ""), params.get("arguments") or {}
            )
            _respond(msg_id, result, error)
        else:
            _respond(msg_id, error={"code": -32601, "message": f"no method {method}"})


if __name__ == "__main__":
    main()
