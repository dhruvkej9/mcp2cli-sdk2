"""Minimal stdio MCP server that mimics schema-less (CLI-argv style) servers.

Some MCP servers — often thin wrappers around CLI tools — publish tools whose
``inputSchema`` declares no properties (or is entirely empty). They accept
arguments as opaque JSON and never map CLI flags to declared parameters.

This raw JSON-RPC implementation (no MCP SDK) reproduces that exact shape so
mcp2cli can be tested against it end-to-end over stdio.
"""

import json
import sys


def _send(msg: dict) -> None:
    sys.stdout.write(json.dumps(msg) + "\n")
    sys.stdout.flush()


def _respond(msg_id, result=None, error=None) -> None:
    if msg_id is None:
        return
    resp = {"jsonrpc": "2.0", "id": msg_id}
    if error is not None:
        resp["error"] = error
    else:
        resp["result"] = result
    _send(resp)


def main() -> None:
    for line in sys.stdin:
        line = line.strip()
        if not line:
            continue
        try:
            msg = json.loads(line)
        except json.JSONDecodeError:
            continue
        method = msg.get("method")
        msg_id = msg.get("id")
        if method == "initialize":
            _respond(
                msg_id,
                {
                    "protocolVersion": (msg.get("params") or {}).get(
                        "protocolVersion", "2024-11-05"
                    ),
                    "capabilities": {"tools": {}},
                    "serverInfo": {"name": "schemaless-server", "version": "0.1.0"},
                },
            )
        elif method == "tools/list":
            _respond(
                msg_id,
                {
                    "tools": [
                        {
                            "name": "run",
                            "description": "Run a CLI-argv command (no declared parameters)",
                            "inputSchema": {"type": "object"},
                        },
                        {
                            "name": "search_docs",
                            "description": "Search docs (no declared parameters)",
                            "inputSchema": {"type": "object"},
                        },
                        {
                            "name": "boom",
                            "description": "Always fails with isError",
                            "inputSchema": {"type": "object"},
                        },
                        {
                            "name": "mcp_error",
                            "description": "Responds with a JSON-RPC error",
                            "inputSchema": {"type": "object"},
                        },
                    ]
                },
            )
        elif method == "tools/call":
            params = msg.get("params") or {}
            name = params.get("name")
            arguments = params.get("arguments") or {}
            if name == "boom":
                _respond(
                    msg_id,
                    {
                        "content": [{"type": "text", "text": "boom exploded"}],
                        "isError": True,
                    },
                )
                continue
            if name == "mcp_error":
                _respond(
                    msg_id,
                    error={
                        "code": -32602,
                        "message": "invalid params: nope",
                    },
                )
                continue
            text = f"called {name} with arguments={json.dumps(arguments, sort_keys=True)}"
            _respond(
                msg_id,
                {
                    "content": [{"type": "text", "text": text}],
                    "isError": False,
                },
            )
        elif method == "ping":
            _respond(msg_id, {})
        else:
            _respond(
                msg_id, error={"code": -32601, "message": f"method not found: {method}"}
            )


if __name__ == "__main__":
    main()