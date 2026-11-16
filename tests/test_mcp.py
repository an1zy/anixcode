import asyncio
import json
import sys
from pathlib import Path

import httpx
import pytest

from anixcode.mcp import HttpConnection, McpManager, McpServer, McpTool, render_result, tool_name
from anixcode.tools.base import ToolContext, ToolError

FAKE = str(Path(__file__).with_name("fake_mcp_server.py"))


def stdio_config(**extra):
    return {"fake": {"command": sys.executable, "args": [FAKE], **extra}}


def test_stdio_server_tools_and_calls(tmp_path):
    async def go():
        mcp = McpManager(stdio_config(), tmp_path)
        await mcp.start()
        try:
            assert mcp.errors == {}
            tools = mcp.tools()
            assert set(tools) == {"mcp__fake__echo", "mcp__fake__fail", "mcp__fake__slow"}  # both pages
            echo = tools["mcp__fake__echo"]
            assert echo.kind == "mcp"
            assert "$schema" not in echo.schema()["function"]["parameters"]
            assert tools["mcp__fake__fail"].parameters == {"type": "object", "properties": {}}
            ctx = ToolContext(tmp_path)
            ok = await echo.run({"text": "привет"}, ctx)
            assert ok.output == "echo: привет" and not ok.error
            bad = await tools["mcp__fake__fail"].run({}, ctx)
            assert bad.error and bad.output == "boom"
            assert mcp.summary() == [("fake", True, "3 tools")]
        finally:
            await mcp.close()

    asyncio.run(go())


def test_allow_list_and_disabled(tmp_path):
    async def go():
        cfg = {**stdio_config(tools=["echo"]), "off": {"command": "nope", "disabled": True}}
        mcp = McpManager(cfg, tmp_path)
        await mcp.start()
        try:
            assert list(mcp.tools()) == ["mcp__fake__echo"]
            assert "off" not in mcp.errors
        finally:
            await mcp.close()

    asyncio.run(go())


def test_timeout_becomes_tool_error(tmp_path):
    async def go():
        mcp = McpManager(stdio_config(timeout=1), tmp_path)
        await mcp.start()
        try:
            with pytest.raises(ToolError, match="no answer"):
                await mcp.tools()["mcp__fake__slow"].run({}, ToolContext(tmp_path))
        finally:
            await mcp.close()

    asyncio.run(go())


def test_broken_servers_are_reported_not_raised(tmp_path):
    async def go():
        cfg = {"missing": {"command": "definitely-not-a-real-command-xyz"}, "empty": {},
               "crash": {"command": sys.executable, "args": ["-c", "import sys; print('bad key', file=sys.stderr)"]}}
        mcp = McpManager(cfg, tmp_path)
        await mcp.start()
        assert set(mcp.errors) == {"missing", "empty", "crash"}
        assert "bad key" in mcp.errors["crash"]  # stderr is shown to the user
        assert mcp.tools() == {}
        await mcp.close()

    asyncio.run(go())


def test_http_server_json_and_sse():
    seen = []

    def handler(request: httpx.Request) -> httpx.Response:
        if request.method == "DELETE":
            seen.append(("DELETE", request.headers.get("mcp-session-id")))
            return httpx.Response(200)
        msg = json.loads(request.content)
        seen.append((msg.get("method"), request.headers.get("mcp-session-id"), request.headers.get("authorization")))
        if msg.get("method") == "initialize":
            return httpx.Response(200, headers={"Mcp-Session-Id": "s1"}, json={
                "jsonrpc": "2.0", "id": msg["id"], "result": {"protocolVersion": "2025-06-18", "capabilities": {}}})
        if "id" not in msg:
            return httpx.Response(202)
        if msg["method"] == "tools/list":
            body = {"jsonrpc": "2.0", "id": msg["id"], "result": {"tools": [{"name": "search.docs"}]}}
        else:
            body = {"jsonrpc": "2.0", "id": msg["id"], "result": {"content": [{"type": "text", "text": "found"}]}}
        sse = f"event: message\ndata: {json.dumps(body)}\n\n"
        return httpx.Response(200, headers={"content-type": "text/event-stream"}, content=sse.encode())

    async def go(monkey_env):
        conn = HttpConnection("https://x.test/mcp", {"Authorization": "Bearer ${TEST_TOKEN}"},
                              client=httpx.AsyncClient(transport=httpx.MockTransport(handler)))
        server = McpServer("docs", conn)
        await server.connect()
        tool = McpTool(server, server.tools[0], tool_name("docs", "search.docs", {}))
        assert tool.name == "mcp__docs__search_docs"
        out = await tool.run({"q": "x"}, ToolContext(Path(".")))
        assert out.output == "found"
        await conn.close()

    import os
    os.environ["TEST_TOKEN"] = "abc"
    asyncio.run(go(None))
    assert seen[0] == ("initialize", None, "Bearer abc")
    assert all(s[1] == "s1" for s in seen[1:])  # the session id is sent after initialize
    assert seen[-1][0] == "DELETE"


def test_names_and_rendering():
    taken = {}
    long = tool_name("server", "x" * 100, taken)
    assert len(long) == 64
    taken[long] = 1
    assert tool_name("server", "x" * 100, taken).endswith("_2")
    assert tool_name("my server", "do/it", {}) == "mcp__my_server__do_it"
    assert render_result({"content": [{"type": "image", "mimeType": "image/png"},
                                      {"type": "resource_link", "name": "a", "uri": "file:///a"}]}) \
        == "[image image/png: not shown]\n\n[link] a file:///a"
    assert render_result({"structuredContent": {"n": 1}}) == '{\n  "n": 1\n}'
    assert render_result({}) == "(no output)"


def test_older_protocol_version_fallback(tmp_path):
    async def go():
        mcp = McpManager(stdio_config(env={"FAKE_ONLY_VERSION": "2024-11-05"}), tmp_path)
        await mcp.start()
        try:
            assert mcp.errors == {} and "mcp__fake__echo" in mcp.tools()
        finally:
            await mcp.close()

    asyncio.run(go())


def test_schema_without_type_keeps_arguments():
    from anixcode.mcp import Connection

    server = McpServer("s", Connection())
    spec = {"name": "t", "inputSchema": {"properties": {"path": {"type": "string"}}, "required": ["path"]}}
    assert McpTool(server, spec, "n").parameters == {
        "properties": {"path": {"type": "string"}}, "required": ["path"], "type": "object"}
    weird = {"name": "t", "inputSchema": {"type": "array"}}
    assert McpTool(server, weird, "n").parameters == {"type": "object", "properties": {}}


def test_stdout_traceback_is_shown(tmp_path):
    async def go():
        cfg = {"crash": {"command": sys.executable, "args": ["-c", "print('ModuleNotFoundError: No module named foo')"]}}
        mcp = McpManager(cfg, tmp_path)
        await mcp.start()
        assert "No module named foo" in mcp.errors["crash"]

    asyncio.run(go())


def test_command_with_env_var(tmp_path):
    import os

    os.environ["ANIX_TEST_PY"] = sys.executable

    async def go():
        mcp = McpManager({"fake": {"command": "${ANIX_TEST_PY}", "args": [FAKE]}}, tmp_path)
        await mcp.start()
        try:
            assert mcp.errors == {}
        finally:
            await mcp.close()

    asyncio.run(go())


def test_http_202_for_a_request_fails_fast():
    import time

    conn = HttpConnection("https://x.test/mcp", {},
                          client=httpx.AsyncClient(transport=httpx.MockTransport(lambda r: httpx.Response(202))))
    start = time.monotonic()
    with pytest.raises(Exception, match="202"):
        asyncio.run(McpServer("s", conn).connect())
    assert time.monotonic() - start < 5


def test_parse_add_local_remote_and_errors():
    from anixcode.mcp import McpError, parse_add

    assert parse_add("fs npx -y @modelcontextprotocol/server-filesystem .") == (
        "fs", {"command": "npx", "args": ["-y", "@modelcontextprotocol/server-filesystem", "."]})
    assert parse_add("gh GITHUB_PERSONAL_ACCESS_TOKEN=${GITHUB_TOKEN} npx -y server-github") == (
        "gh", {"command": "npx", "args": ["-y", "server-github"],
               "env": {"GITHUB_PERSONAL_ACCESS_TOKEN": "${GITHUB_TOKEN}"}})
    assert parse_add("ctx https://mcp.context7.com/mcp") == ("ctx", {"url": "https://mcp.context7.com/mcp"})
    assert parse_add('docs https://x.test/mcp "Authorization: Bearer ${T}"') == (
        "docs", {"url": "https://x.test/mcp", "headers": {"Authorization": "Bearer ${T}"}})
    for bad in ["", "onlyname", "bad/name npx x", "gh TOKEN=1", "d https://x.test/mcp nocolon",
                "d TOKEN=1 https://x.test/mcp", 'gh "unclosed']:
        with pytest.raises(McpError):
            parse_add(bad)


def test_status_rows(tmp_path):
    from anixcode.mcp import status_rows

    async def go():
        cfg = {**stdio_config(), "broken": {"command": "definitely-not-a-real-command-xyz"},
               "off": {"url": "https://x.test/mcp", "disabled": True}}
        assert [r[1] for r in status_rows(cfg, None)] == ["starting", "starting", "off"]
        mcp = McpManager(cfg, tmp_path)
        await mcp.start()
        try:
            rows = {r[0]: r for r in status_rows({**cfg, "new": {"command": "x"}}, mcp)}
            assert rows["fake"][1:3] == ("ok", "3")
            assert rows["broken"][1] == "error"
            assert rows["off"] == ("off", "off", "", "https://x.test/mcp")
            assert rows["new"][1] == "starting"  # added, the restart is not done yet
        finally:
            await mcp.close()

    asyncio.run(go())
