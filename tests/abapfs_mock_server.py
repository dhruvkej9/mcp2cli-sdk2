"""Mock of the ABAP FS MCP server (marcellourbani/vscode_abap_remote_fs).

Modelled on `client/src/services/mcpServer.ts` in that repo, which is a real,
widely-used MCP server with a wire contract mcp2cli had never been tested
against:

* **Stateful streamable HTTP.** ``initialize`` mints an ``Mcp-Session-Id``;
  every later POST must carry it. A POST without one that is not an initialize
  gets ``400 -32000 "Bad Request: No valid session ID provided"``, and GET/DELETE
  without a live session get 400 too. A client that drops the session header
  works for exactly one request and then fails — which is precisely the "tool
  calls fail even though listing worked" failure mode.
* **Two accepted credential formats.** Its `validateApiKey` takes either
  ``Authorization: Bearer <key>`` *or* a bare ``Authorization: <key>``.
* **A JSON-RPC error body on 401** (``-32001``), not a plain HTTP error.
* **An unauthenticated ``/health`` endpoint.**
* Tool schemas copied from the extension's real `languageModelTools` entries:
  enum-constrained strings, arrays of enums, numbers with defaults, and a
  no-argument tool.

Written against ``http.server`` rather than the MCP SDK so the session and auth
behaviour is exactly what the ABAP server does, not what the SDK would do.

Usage: ``python abapfs_mock_server.py [--api-key KEY] [--no-auth]``; prints
``PORT=<port>`` once listening.
"""

import argparse
import json
import socket
import sys
import threading
import uuid
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

PROTOCOL_FALLBACK = "2025-06-18"

# Schemas lifted from the extension's package.json (trimmed to the shapes that
# matter for CLI generation: enums, enum arrays, numeric defaults, no-arg).
TOOLS = [
    {
        "name": "get_test_folder",
        "description": "Get the configured SAP testing folder path.",
        "inputSchema": {"type": "object", "properties": {}},
    },
    {
        "name": "get_sap_webgui_url",
        "description": "Return a ready-to-open SAP WebGUI URL for a connection.",
        "inputSchema": {
            "type": "object",
            "properties": {
                "connectionId": {"type": "string", "description": "SAP connection ID"},
                "transaction": {"type": "string", "description": "Transaction code"},
            },
            "required": ["connectionId"],
        },
    },
    {
        "name": "search_abap_objects",
        "description": "Search ABAP objects by name pattern. Wildcards: * ?",
        "inputSchema": {
            "type": "object",
            "properties": {
                "pattern": {"type": "string", "description": "Search pattern"},
                "types": {
                    "type": "array",
                    "items": {
                        "type": "string",
                        "enum": ["FUNC", "CLAS", "TABL", "PROG", "INTF", "DTEL"],
                    },
                    "description": "Object types to search for",
                },
                "maxResults": {
                    "type": "number",
                    "default": 20,
                    "description": "Maximum number of results",
                },
                "connectionId": {"type": "string", "description": "SAP connection ID"},
            },
            "required": ["pattern", "types", "connectionId"],
        },
    },
    {
        "name": "get_abap_object_lines",
        "description": "Read active ABAP source.",
        "inputSchema": {
            "type": "object",
            "properties": {
                "objectName": {"type": "string", "description": "Object name"},
                "objectType": {
                    "type": "string",
                    "enum": ["FUNC", "CLAS", "TABL", "PROG", "INTF", "DTEL"],
                    "description": "Object type",
                },
                "startLine": {"type": "number", "description": "First line"},
                "endLine": {"type": "number", "description": "Last line"},
                "connectionId": {"type": "string", "description": "SAP connection ID"},
            },
            "required": ["objectName", "connectionId"],
        },
    },
    {
        "name": "search_abap_object_lines",
        "description": "Search text inside ABAP source. Literal or regex.",
        "inputSchema": {
            "type": "object",
            "properties": {
                "objectName": {"type": "string", "description": "Object to search"},
                "searchString": {"type": "string", "description": "Text to find"},
                "isRegexp": {
                    "type": "boolean",
                    "default": False,
                    "description": "Treat searchString as a regular expression",
                },
                "maxObjects": {
                    "type": "number",
                    "default": 10,
                    "description": "Maximum objects to scan",
                },
                "connectionId": {"type": "string", "description": "SAP connection ID"},
            },
            "required": ["objectName", "searchString", "connectionId"],
        },
    },
]

SESSIONS: set[str] = set()
API_KEY = ""


def _authorized(headers) -> bool:
    """Mirror the extension's validateApiKey: Bearer <key> or a bare key."""
    if not API_KEY:
        return True
    supplied = headers.get("Authorization")
    if not supplied:
        return False
    token = supplied[7:] if supplied.startswith("Bearer ") else supplied
    return token == API_KEY


def _dispatch(message: dict) -> dict | None:
    """Handle one JSON-RPC message; None means 'notification, nothing to send'."""
    method = message.get("method", "")
    msg_id = message.get("id")
    params = message.get("params") or {}

    if msg_id is None:
        return None  # notification

    def ok(result):
        return {"jsonrpc": "2.0", "id": msg_id, "result": result}

    def err(code, msg):
        return {"jsonrpc": "2.0", "id": msg_id, "error": {"code": code, "message": msg}}

    if method == "initialize":
        return ok(
            {
                "protocolVersion": params.get("protocolVersion", PROTOCOL_FALLBACK),
                "capabilities": {"tools": {"listChanged": True}},
                "serverInfo": {"name": "abap-fs-mcp", "version": "1.0.0"},
            }
        )
    if method == "ping":
        return ok({})
    if method == "tools/list":
        return ok({"tools": TOOLS})
    if method == "tools/call":
        name = params.get("name", "")
        args = params.get("arguments") or {}
        known = {t["name"] for t in TOOLS}
        if name not in known:
            return err(-32602, f"Unknown tool: {name}")
        if name == "search_abap_objects":
            missing = [k for k in ("pattern", "types", "connectionId") if k not in args]
            if missing:
                return ok(
                    {
                        "content": [
                            {"type": "text", "text": f"Missing required: {missing}"}
                        ],
                        "isError": True,
                    }
                )
        return ok(
            {
                "content": [
                    {"type": "text", "text": json.dumps({"tool": name, "args": args})}
                ]
            }
        )
    return err(-32601, f"Method not found: {method}")


class Handler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"

    def _json(self, status: int, payload: dict, extra_headers: dict | None = None):
        body = json.dumps(payload).encode()
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        for key, value in (extra_headers or {}).items():
            self.send_header(key, value)
        self.end_headers()
        self.wfile.write(body)

    def _unauthorized(self):
        self._json(
            401,
            {
                "jsonrpc": "2.0",
                "error": {
                    "code": -32001,
                    "message": (
                        "Unauthorized: Invalid or missing API key. Set the API key "
                        "in the Authorization header (Bearer <token>)."
                    ),
                },
                "id": None,
            },
        )

    def _bad_session(self):
        self._json(
            400,
            {
                "jsonrpc": "2.0",
                "error": {"code": -32000, "message": "Bad Request: No valid session ID provided"},
                "id": None,
            },
        )

    def do_GET(self):  # noqa: N802 - BaseHTTPRequestHandler API
        if self.path == "/health":  # unauthenticated, like the real server
            self._json(200, {"status": "ok", "server": "abap-fs-mcp"})
            return
        if not _authorized(self.headers):
            self._unauthorized()
            return
        session = self.headers.get("Mcp-Session-Id")
        if not session or session not in SESSIONS:
            self._json(400, {"error": "Invalid or missing session ID"})
            return
        # The real server opens an SSE stream here; nothing to push in a mock.
        self.send_response(200)
        self.send_header("Content-Type", "text/event-stream")
        self.send_header("Cache-Control", "no-cache")
        self.send_header("Content-Length", "0")
        self.end_headers()

    def do_DELETE(self):  # noqa: N802
        if not _authorized(self.headers):
            self._unauthorized()
            return
        session = self.headers.get("Mcp-Session-Id")
        if not session or session not in SESSIONS:
            self._json(400, {"error": "Invalid or missing session ID"})
            return
        SESSIONS.discard(session)
        self._json(200, {"status": "terminated"})

    def do_POST(self):  # noqa: N802
        if not _authorized(self.headers):
            self._unauthorized()
            return
        if self.path.split("?")[0] != "/mcp":
            self._json(404, {"error": "not found"})
            return

        length = int(self.headers.get("Content-Length") or 0)
        raw = self.rfile.read(length) if length else b""
        try:
            message = json.loads(raw or b"{}")
        except json.JSONDecodeError:
            self._json(400, {"error": "invalid json"})
            return

        batch = message if isinstance(message, list) else [message]
        is_initialize = any(m.get("method") == "initialize" for m in batch)
        session = self.headers.get("Mcp-Session-Id")

        if session and session in SESSIONS:
            pass  # established session
        elif not session and is_initialize:
            session = str(uuid.uuid4())
            SESSIONS.add(session)
        else:
            self._bad_session()
            return

        responses = [r for r in (_dispatch(m) for m in batch) if r is not None]
        headers = {"Mcp-Session-Id": session}
        if not responses:
            self.send_response(202)
            self.send_header("Content-Length", "0")
            for key, value in headers.items():
                self.send_header(key, value)
            self.end_headers()
            return
        payload = responses[0] if len(responses) == 1 else responses
        self._json(200, payload, headers)

    def log_message(self, *args):
        pass


def find_free_port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


def main() -> None:
    global API_KEY

    parser = argparse.ArgumentParser()
    parser.add_argument("--api-key", default="abap-secret")
    parser.add_argument("--no-auth", action="store_true",
                        help="mimic the server's unconfigured-key mode (allow all)")
    args = parser.parse_args()
    API_KEY = "" if args.no_auth else args.api_key

    port = find_free_port()
    server = ThreadingHTTPServer(("127.0.0.1", port), Handler)
    print(f"PORT={port}", flush=True)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        thread.join()
    except KeyboardInterrupt:  # pragma: no cover
        server.shutdown()


if __name__ == "__main__":
    main()
