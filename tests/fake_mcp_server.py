"""A tiny stdio MCP server for the tests: echo, fail, slow; tools/list in two pages."""
import json
import os
import sys
import time

TOOLS = [
    {"name": "echo", "description": "Echo the text back", "inputSchema": {
        "$schema": "http://json-schema.org/draft-07/schema#", "type": "object",
        "properties": {"text": {"type": "string"}}, "required": ["text"]}},
    {"name": "fail", "description": "Always fails"},
    {"name": "slow", "description": "Sleeps", "inputSchema": {"type": "object", "properties": {}}},
]


def send(msg):
    sys.stdout.write(json.dumps(msg) + "\n")
    sys.stdout.flush()


print("this is a log line, not JSON", flush=True)
print("starting fake server", file=sys.stderr, flush=True)
for line in sys.stdin:
    msg = json.loads(line)
    method, rid = msg.get("method"), msg.get("id")
    if method == "initialize" and os.environ.get("FAKE_ONLY_VERSION") not in (None, msg["params"]["protocolVersion"]):
        send({"jsonrpc": "2.0", "id": rid, "error": {"code": -32602, "message": "Unsupported protocol version"}})
    elif method == "initialize":
        send({"jsonrpc": "2.0", "id": rid, "result": {
            "protocolVersion": msg["params"]["protocolVersion"], "capabilities": {"tools": {}},
            "serverInfo": {"name": "fake", "version": "1"}}})
    elif method == "tools/list":
        page2 = msg["params"].get("cursor") == "p2"
        result = {"tools": TOOLS[2:] if page2 else TOOLS[:2]}
        if not page2:
            result["nextCursor"] = "p2"
        send({"jsonrpc": "2.0", "id": rid, "result": result})
    elif method == "tools/call":
        name, args = msg["params"]["name"], msg["params"].get("arguments", {})
        if name == "echo":
            send({"jsonrpc": "2.0", "id": 900, "method": "ping"})  # the server pings us mid-call
            send({"jsonrpc": "2.0", "method": "notifications/message", "params": {"level": "info", "data": "hi"}})
            send({"jsonrpc": "2.0", "id": rid, "result": {"content": [{"type": "text", "text": "echo: " + args["text"]}]}})
        elif name == "fail":
            send({"jsonrpc": "2.0", "id": rid, "result": {"content": [{"type": "text", "text": "boom"}], "isError": True}})
        elif name == "slow":
            time.sleep(5)
            send({"jsonrpc": "2.0", "id": rid, "result": {"content": []}})
        else:
            send({"jsonrpc": "2.0", "id": rid, "error": {"code": -32602, "message": "unknown tool"}})
    elif rid is not None and "method" not in msg:
        pass  # our ping answer
