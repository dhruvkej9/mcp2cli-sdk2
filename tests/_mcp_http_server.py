"""Minimal MCP HTTP server for testing (MCP SDK 2.0)."""

import asyncio
import socket

from mcp.server.mcpserver import MCPServer

app = MCPServer("test-http-server")


@app.tool()
async def echo(message: str) -> str:
    """Echo back the input."""
    return message


@app.tool()
async def add_numbers(a: int, b: int) -> int:
    """Add two numbers."""
    return a + b


@app.resource("file:///test/doc.txt", name="Test Document", description="A test text document", mime_type="text/plain")
async def test_doc() -> str:
    """A test text document."""
    return "Hello from test document!"


@app.resource("file:///test/{name}.txt", name="Test Template", description="A text file by name", mime_type="text/plain")
async def test_template(name: str) -> str:
    """A text file by name."""
    return f"Content of {name}"


@app.prompt()
async def greeting(name: str = "World", style: str = "friendly") -> str:
    """Generate a greeting message."""
    return f"Please greet {name} in a {style} way."


@app.tool()
async def greet() -> str:
    """Greet (declares no parameters)."""
    return "hello"


def find_free_port():
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


async def main():
    port = find_free_port()
    print(f"PORT={port}", flush=True)
    await app.run_sse_async(host="127.0.0.1", port=port)


if __name__ == "__main__":
    asyncio.run(main())
