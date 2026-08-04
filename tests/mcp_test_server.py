"""Minimal MCP server for testing — runs over stdio (MCP SDK 2.0)."""

import asyncio
import json

from mcp.server.mcpserver import MCPServer

app = MCPServer("test-server")


@app.tool()
async def echo(message: str) -> str:
    """Echo back the input."""
    return message


@app.tool()
async def add_numbers(a: int, b: int) -> int:
    """Add two numbers."""
    return a + b


@app.tool()
async def list_items(path: str, recursive: bool = False) -> str:
    """List items in a directory (test tool)."""
    return json.dumps(
        {"path": path, "recursive": recursive, "items": ["file1.txt", "file2.txt"]}
    )


@app.tool()
async def deploy(env: str, refresh: bool = False) -> str:
    """Deploy to an environment (shadows global --env and --refresh)."""
    return json.dumps({"env": env, "refresh": refresh})


@app.resource("file:///test/doc.txt", name="Test Document", description="A test text document", mime_type="text/plain")
async def test_doc() -> str:
    """A test text document."""
    return "Hello from test document!"


@app.resource("file:///test/data.bin", name="Binary Data", description="A test binary resource", mime_type="application/octet-stream")
async def test_data() -> bytes:
    """A binary test resource."""
    return b"\x00\x01\x02\x03"


@app.resource("file:///test/{name}.txt", name="Test Template", description="A text file by name", mime_type="text/plain")
async def test_template(name: str) -> str:
    """A text file by name."""
    return f"Content of {name}"


@app.prompt()
async def greeting(name: str = "World", style: str = "friendly") -> str:
    """Generate a greeting message."""
    return f"Please greet {name} in a {style} way."


@app.prompt()
async def summary(topic: str = "general") -> str:
    """Summarize content."""
    return f"Please summarize the topic: {topic}"


async def main():
    await app.run_stdio_async()


if __name__ == "__main__":
    asyncio.run(main())
