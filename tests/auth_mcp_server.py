"""An MCP HTTP server that requires a bearer token on *every* request.

Real hosted MCP servers are almost always authenticated, and the failure that
matters is not `--list` — it is the **tool call**, which travels on a different
request (and, over SSE, a different endpoint) than the handshake. A client that
only attaches credentials to the first request lists tools happily and then
fails on every call. This server makes that mistake impossible to miss: the
middleware sits in front of the whole ASGI app, so the SSE GET, the SSE message
POST and every streamable-HTTP POST are all checked independently.

Usage::

    python auth_mcp_server.py [streamable|sse] [--token TOKEN] [--header NAME]

Prints ``PORT=<port>`` on stdout once listening.
"""

import argparse
import asyncio
import json
import socket

from mcp.server.mcpserver import MCPServer

app = MCPServer("auth-test-server")

# Set from argv in main(); read by the middleware and by the whoami tool.
EXPECTED_TOKEN = "sk-test-123"
AUTH_HEADER = "authorization"
SEEN_HEADERS: dict[str, str] = {}


@app.tool()
async def echo(message: str) -> str:
    """Echo back the input."""
    return message


@app.tool()
async def add_numbers(a: int, b: int) -> int:
    """Add two numbers."""
    return a + b


@app.tool()
async def whoami() -> str:
    """Report the credential the server saw on the most recent request."""
    return json.dumps({"header": AUTH_HEADER, "value": SEEN_HEADERS.get(AUTH_HEADER, "")})


@app.tool()
async def explode() -> str:
    """Always fails, to prove errors survive the authenticated path."""
    raise ValueError("tool failed on purpose")


@app.resource(
    "file:///secret/doc.txt",
    name="Secret Document",
    description="Only readable with a valid token",
    mime_type="text/plain",
)
async def secret_doc() -> str:
    """A resource behind the same auth."""
    return "classified payload"


@app.prompt()
async def secret_prompt(topic: str = "security") -> str:
    """A prompt behind the same auth."""
    return f"Tell me about {topic}."


class BearerAuthMiddleware:
    """Reject any request without the expected credential, ASGI-level.

    Written as raw ASGI rather than Starlette's BaseHTTPMiddleware so that SSE
    responses stream through untouched.
    """

    def __init__(self, app):
        self.app = app

    async def __call__(self, scope, receive, send):
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return

        headers = {
            k.decode("latin-1").lower(): v.decode("latin-1")
            for k, v in scope.get("headers", [])
        }
        SEEN_HEADERS.clear()
        SEEN_HEADERS.update(headers)
        supplied = headers.get(AUTH_HEADER, "")
        expected = (
            f"Bearer {EXPECTED_TOKEN}" if AUTH_HEADER == "authorization" else EXPECTED_TOKEN
        )
        if supplied != expected:
            body = json.dumps(
                {"error": "unauthorized", "detail": f"{AUTH_HEADER} header missing or wrong"}
            ).encode()
            await send(
                {
                    "type": "http.response.start",
                    "status": 401,
                    "headers": [
                        (b"content-type", b"application/json"),
                        (b"content-length", str(len(body)).encode()),
                        (b"www-authenticate", b'Bearer realm="mcp2cli-test"'),
                    ],
                }
            )
            await send({"type": "http.response.body", "body": body})
            return

        await self.app(scope, receive, send)


def find_free_port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


async def main() -> None:
    global EXPECTED_TOKEN, AUTH_HEADER

    parser = argparse.ArgumentParser()
    parser.add_argument("transport", nargs="?", default="streamable",
                        choices=["streamable", "sse"])
    parser.add_argument("--token", default="sk-test-123")
    parser.add_argument("--header", default="authorization")
    args = parser.parse_args()

    EXPECTED_TOKEN = args.token
    AUTH_HEADER = args.header.lower()

    import uvicorn

    inner = (
        app.streamable_http_app() if args.transport == "streamable" else app.sse_app()
    )
    port = find_free_port()
    print(f"PORT={port}", flush=True)
    config = uvicorn.Config(
        BearerAuthMiddleware(inner), host="127.0.0.1", port=port, log_level="error"
    )
    await uvicorn.Server(config).serve()


if __name__ == "__main__":
    asyncio.run(main())
