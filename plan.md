# plan.md — make mcp2cli work with any MCP server, end to end

**Status: complete.** Every item below is implemented, tested, and verified
against real servers. Nothing is deferred except the two items under
"Out of scope", which are called out rather than silently dropped.

Baseline before this work (2026-08-14): `uv run pytest tests/` → **369 passed**,
all against in-repo fakes. None of the failures below were caught by them.
After: **505 hermetic tests pass** (32 real-server tests skipped by default),
and `MCP2CLI_TEST_REAL=1 uv run pytest tests/test_real_servers.py` → **32
passed** against the real npx servers over stdio, streamable HTTP and SSE.

---

## How the bugs were found

The CLI was run against real, published MCP servers, and against a
purpose-built "hostile but protocol-legal" server:

| Target | Transport |
| --- | --- |
| `@modelcontextprotocol/server-everything` | stdio, streamable HTTP, SSE |
| `@modelcontextprotocol/server-filesystem` | stdio |
| `@modelcontextprotocol/server-memory` | stdio (array-of-objects params) |
| `@modelcontextprotocol/server-sequential-thinking` | stdio |
| `mcp-server-time`, `mcp-server-git` pinned to `mcp==1.9.0` | stdio — genuine **MCP 1.x** servers with pydantic schemas |
| `@playwright/mcp` | stdio — a large third-party server (24 tools) |
| `mcp.context7.com/mcp`, `mcp.deepwiki.com/mcp` | hosted production servers over streamable HTTP |
| `tests/hostile_server.py` | raw JSON-RPC, every awkward-but-legal shape |

Two servers (`mcp-server-time`, `mcp-server-git` unpinned) fail on their own
bug — their published code calls `McpError` / `Server.list_tools`, which no
longer exist in the `mcp` 2.x they resolve to. mcp2cli now reports exactly that
("closed the connection before the handshake completed … check the server's own
stderr") instead of a traceback, and works fine against the same servers pinned
to a version that starts.

---

## P0 — confirmed breakage against real servers

- [x] **1. `httpx` vs `httpx2`.** The MCP SDK 2.0 is built on `httpx2` 2.10;
  mcp2cli used `httpx` 0.28. Verified consequences:
  - `httpx.Client(auth=<SDK OAuth provider>)` → `TypeError: Invalid "auth"
    argument`, i.e. **OAuth was completely dead in `--spec`/`--graphql` mode**.
  - Every `isinstance(exc, httpx.*)` check against transport errors was
    permanently false.
  Fixed by moving mcp2cli's HTTP layer onto `httpx2` (already an `mcp`
  dependency) and declaring it in `pyproject.toml`.

- [x] **2. Transport auto-fallback never fired.** Failures arrive as
  `ExceptionGroup(ExceptionGroup(MCPError(-32601)))`, so the old single-level
  `isinstance` check never matched and an SSE-only server at a non-`/sse` URL
  died with a traceback. Fixed by flattening exception groups
  (`_iter_exceptions`) and matching on status/JSON-RPC code.
  **Extra wrinkle found:** the SDK collapses 400/401/403/405/500 into one
  opaque `MCPError(-32603, "Server returned an error response")` — the HTTP
  status is gone. So on a failed streamable attempt in `auto` mode mcp2cli now
  probes the endpoint itself (`_probe_http_status`) and retries over SSE unless
  the endpoint answered 401/403/407. If both transports fail, the *streamable*
  error is reported, since that is the endpoint the user asked for.

- [x] **3. Pagination ignored.** `tools/list`, `resources/list`,
  `resources/templates/list` and `prompts/list` all page with `nextCursor`;
  mcp2cli read page 1 only, so on a paginating server every later tool was
  invisible *and* uncallable. One `_paginate()` helper now drives all four,
  with repeat-cursor and empty-page guards so a buggy server cannot spin it.

- [x] **4. Kebab-case collisions bricked the CLI.** A server exposing both
  `get_user` and `getUser` → `ValueError: conflicting subparser: get-user` on
  *every* invocation, `--help` included. Now de-duplicated to `get-user` /
  `get-user-2`, with the original name still sent on the wire.

- [x] **5. A property named `help`/`stdin`/`json` bricked the CLI.**
  `argparse.ArgumentError: conflicting option string: --help` — raised while
  building the parser, so one hostile tool disabled *every* tool on that
  server. Reserved names are renamed (`--arg-help`), and subparser construction
  is per-flag fault-tolerant so nothing can take the whole CLI down again.

- [x] **6. Numeric `enum` without `type` was uncallable.** `--level 2` →
  `invalid choice: '2' (choose from '1', '2', '3')`. The type is now inferred
  from enum members and choices are coerced to the flag's type.

- [x] **7. Union types sent the wrong JSON type.** `{"type":["integer","null"]}`
  and `anyOf`/`oneOf` degraded to strings, so `--count 5` sent `"5"` and strict
  pydantic/zod servers rejected it. `effective_schema_type()` now resolves
  union lists, `anyOf`/`oneOf`/`allOf`, and bare enums for both flag typing and
  value coercion.

- [x] **8. `structuredContent`-only results printed nothing.** Tools with an
  `outputSchema` may return `content: []`; output now falls back to the
  structured payload.

- [x] **9. `resource_link` and embedded `resource` blocks were dropped.**
  `get-resource-links` printed its preamble and silently lost every link.
  All block kinds now render (text, image/audio data, link URIs, embedded
  text/blob).

- [x] **10. Raw `ExceptionGroup` tracebacks on every non-tool failure.** Bad
  URL, unknown resource, unknown prompt, server dying at startup — all dumped
  40+ lines. Now one `Error: …` line and exit 1, with `MCP2CLI_DEBUG=1` to get
  the traceback back.

- [x] **11. One out-of-spec tool made an entire server unusable.** The SDK
  validates each response against the method's schema, so a single tool with
  `inputSchema: null` — *or even `{}`, which is what CLI-wrapper servers
  publish, and which the README already claimed to support* — makes the whole
  `tools/list` fail. mcp2cli now falls back to an unvalidated read through the
  dispatcher and salvages every usable entry. The same fallback covers
  `resources/list`, `resources/templates/list`, `prompts/list`, `tools/call`,
  `resources/read` and `prompts/get`, so a malformed content block loses one
  block instead of the whole result.

- [x] **12. `--head` was silently ignored** on every MCP tool call, OpenAPI
  call and GraphQL call — it was never threaded past the parser. Now passed on
  all three paths, and it truncates text to N lines as documented.

## P1 — robustness and UX

- [x] **13. Booleans could only ever be sent as `true`.** Now `--flag` /
  `--no-flag` via `argparse.BooleanOptionalAction`.
- [x] **14. Tool names that are not valid CLI words** (`browser/navigate`,
  `Slack.postMessage`, `search files`) are sanitized into typable commands.
- [x] **15. Two properties kebab-ing to the same flag** (`fooBar`, `foo_bar`)
  silently dropped one; they now become `--foo-bar` and `--foo-bar-2`.
- [x] **16. A raw escape hatch on every MCP tool** — `--stdin`, `--json '{…}'`
  and repeatable `--arg K=V` used to exist only for schema-less tools. Any tool
  whose schema cannot be expressed as flags is now still callable.
- [x] **17. No client identity.** mcp2cli now sends
  `clientInfo = {name: "mcp2cli", version: …}`.
- [x] **18. No timeout control.** Added `--timeout SECONDS` (default 300),
  applied to MCP requests, SSE reads, and OpenAPI/GraphQL HTTP calls.
- [x] **19. Task-augmented tools** (`taskSupport: required`) now get an
  actionable message instead of a bare JSON-RPC code.
- [x] **20. A stdio server that dies at startup** reports what happened and
  points at the server's own stderr.
- [x] **21. Servers that print a banner on stdout** before speaking JSON-RPC
  still work, and stdout stays machine-parseable.
- [x] **22. The session daemon** shares all of the above through the same
  helpers rather than carrying a second copy of the logic. Two session-only
  defects fell out of testing it:
  - a deep `MCP2CLI_CACHE_DIR` (or home directory) pushed the Unix socket past
    the AF_UNIX length cap and the daemon died with `AF_UNIX path too long`;
    it now falls back to a short, deterministic path under the temp dir;
  - `--compact`, `--top`, `--sort` and `--head` were silently ignored in
    session mode — exactly the flags that keep agent token cost down.

---

## Testing overhaul

- [x] `tests/hostile_server.py` — raw JSON-RPC server with modes `all`,
  `malformed`, `noisy`, `onepage`, `empty`, `crash`, covering paginated lists,
  colliding names, reserved flag names, int/mixed enums, union types,
  `structuredContent`-only results, `resource_link` + embedded resources,
  `isError`, JSON-RPC errors, slow tools, and payloads the MCP schema rejects.
- [x] `tests/test_robustness.py` — 96 tests, one or more per item above.
- [x] `tests/test_transport_fallback.py` — 21 tests: fake 405/401 endpoints
  prove the fallback ladder, that auth failures never trigger a retry, and that
  the SDK's OAuth provider is accepted by our HTTP client.
- [x] `tests/test_sessions.py` — 19 tests proving the persistent session daemon
  behaves identically to a direct connection (pagination, content blocks, clean
  errors, list shaping) plus the socket-path length fallback.
- [x] `tests/test_real_servers.py` — opt-in integration suite against the real
  everything / filesystem / memory / playwright / git servers over stdio,
  streamable HTTP and SSE, plus a genuine MCP 1.x server and a bake round-trip. Skipped unless
  `MCP2CLI_TEST_REAL=1`, so the default suite stays hermetic.
- [x] `pyproject.toml` — registered markers (`real`, `slow`),
  `--strict-markers`, `asyncio_mode`, `testpaths`.
- [x] README documents the robustness guarantees, `--timeout`, `MCP2CLI_DEBUG`,
  the escape hatch, and how the test suite is organised.

### Running the tests

```bash
uv sync --extra test
uv run pytest tests/                       # hermetic, no network
uv run pytest tests/test_robustness.py -v  # the robustness suite alone
MCP2CLI_TEST_REAL=1 uv run pytest tests/test_real_servers.py -v   # real servers
```

---

## Out of scope (stated, not silently dropped)

- **MCP tasks** (`taskSupport: required`, protocol 2025-11-25). The installed
  `mcp` 2.0.0 exposes no client API for task-augmented calls, so mcp2cli
  reports the limitation clearly instead of failing with a raw JSON-RPC code.
  Revisit when the SDK ships it.
- **Sampling / elicitation callbacks** — a CLI has no model to sample from.
- **Live OAuth against a third-party IdP** — the flows are exercised against
  the SDK's own machinery; a real IdP round-trip needs credentials this repo
  does not have.
