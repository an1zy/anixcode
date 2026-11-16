"""MCP servers: start or connect to them and give their tools to the agent.

settings.json -> "mcp_servers" (the same shape as "mcpServers" in Claude Desktop / Cursor):

    "mcp_servers": {
      "github":   {"command": "npx", "args": ["-y", "@modelcontextprotocol/server-github"],
                   "env": {"GITHUB_PERSONAL_ACCESS_TOKEN": "${GITHUB_TOKEN}"}},
      "context7": {"url": "https://mcp.context7.com/mcp"},
      "big":      {"command": "uvx", "args": ["some-server"], "tools": ["search", "fetch"], "timeout": 300},
      "old":      {"command": "...", "disabled": true}
    }

Local servers (command) talk over stdio and start in the project folder; remote ones (url) use
Streamable HTTP. ${VAR} in args, env, url and headers comes from the environment, so tokens don't
have to be written into settings.json. "tools" keeps only the listed tools (saves context).
"""
from __future__ import annotations

import asyncio
import collections
import json
import os
import re
import shlex
import shutil
import signal
import subprocess
import sys
from collections.abc import AsyncIterator
from pathlib import Path
from typing import Any

import httpx

from . import __version__
from .i18n import tr
from .tools.base import Tool, ToolContext, ToolError, ToolResult, truncate

# Newest first. The spec says a server answers with its own version when it does not know ours,
# but some servers reject an unknown version with an error instead: then we retry with older ones.
SUPPORTED_VERSIONS = ("2025-06-18", "2025-03-26", "2024-11-05")
PROTOCOL_VERSION = SUPPORTED_VERSIONS[0]
START_TIMEOUT = 60.0  # npx / uvx may download the server on the first start
CALL_TIMEOUT = 120.0
_UNSAFE = re.compile(r"[^a-zA-Z0-9_-]")  # tool names allowed by OpenAI-compatible APIs
_SERVER_NAME = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_-]{0,40}$")
_ENV_PAIR = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*=")


class McpError(Exception):
    """A server could not start, broke the protocol or answered with an error."""


def _expand(value: Any) -> str:
    return os.path.expandvars(str(value))


# -- JSON-RPC ------------------------------------------------------------------------------
class Connection:
    """JSON-RPC 2.0 over a transport: request ids, waiting for answers, replies to server requests."""

    def __init__(self) -> None:
        self._next_id = 0
        self._pending: dict[int, asyncio.Future[dict[str, Any]]] = {}

    async def start(self) -> None:
        pass

    async def close(self) -> None:
        pass

    async def _send(self, msg: dict[str, Any]) -> None:
        raise NotImplementedError

    def detail(self) -> str:
        """Extra text for error messages (e.g. the server's last stderr lines)."""
        return ""

    async def request(self, method: str, params: dict[str, Any] | None = None,
                      timeout: float = CALL_TIMEOUT) -> dict[str, Any]:
        self._next_id += 1
        rid = self._next_id
        fut: asyncio.Future[dict[str, Any]] = asyncio.get_running_loop().create_future()
        self._pending[rid] = fut

        async def roundtrip() -> dict[str, Any]:
            await self._send({"jsonrpc": "2.0", "id": rid, "method": method, "params": params or {}})
            return await fut

        try:
            return await asyncio.wait_for(roundtrip(), timeout)
        except asyncio.TimeoutError:
            await self._cancel(rid, "timeout")
            raise McpError(f"no answer to {method} in {timeout:.0f}s{self.detail()}") from None
        except asyncio.CancelledError:  # Esc in the interface: tell the server to stop too
            await self._cancel(rid, "cancelled by the user")
            raise
        finally:
            self._pending.pop(rid, None)

    async def notify(self, method: str, params: dict[str, Any] | None = None) -> None:
        msg: dict[str, Any] = {"jsonrpc": "2.0", "method": method}
        if params:
            msg["params"] = params
        await self._send(msg)

    async def _cancel(self, rid: int, reason: str) -> None:
        try:
            await asyncio.wait_for(self.notify("notifications/cancelled", {"requestId": rid, "reason": reason}), 2)
        except (McpError, asyncio.TimeoutError, OSError):
            pass

    def _fail_all(self, error: McpError) -> None:
        for fut in self._pending.values():
            if not fut.done():
                fut.set_exception(error)

    async def _dispatch(self, msg: Any) -> None:
        if isinstance(msg, list):  # a batch
            for m in msg:
                await self._dispatch(m)
            return
        if not isinstance(msg, dict):
            return
        if "method" not in msg:  # an answer to one of our requests
            rid = msg.get("id")
            if isinstance(rid, str) and rid.isdigit():
                rid = int(rid)
            fut = self._pending.get(rid)  # type: ignore[arg-type]
            if fut is None or fut.done():
                return
            if "error" in msg:
                err = msg["error"] if isinstance(msg["error"], dict) else {"message": str(msg["error"])}
                fut.set_exception(McpError(f"{err.get('message', 'error')} (code {err.get('code', '?')})"))
            else:
                result = msg.get("result")
                fut.set_result(result if isinstance(result, dict) else {})
        elif "id" in msg:  # the server asks us something
            reply: dict[str, Any] = {"jsonrpc": "2.0", "id": msg["id"]}
            if msg["method"] == "ping":
                reply["result"] = {}
            else:  # sampling, roots, elicitation: not supported yet
                reply["error"] = {"code": -32601, "message": f"ANIX Code does not support {msg['method']}"}
            try:
                await self._send(reply)
            except McpError:
                pass
        # notifications (logs, progress, list_changed) are ignored for now


def _kill_tree(proc: asyncio.subprocess.Process) -> None:
    try:
        if sys.platform == "win32":
            subprocess.run(["taskkill", "/F", "/T", "/PID", str(proc.pid)], capture_output=True, check=False)
        else:
            os.killpg(proc.pid, signal.SIGKILL)
    except (OSError, ProcessLookupError):
        pass


class StdioConnection(Connection):
    """A local server: a child process, one JSON message per line on stdin/stdout."""

    def __init__(self, command: str, args: list[str], env: dict[str, Any], cwd: Path) -> None:
        super().__init__()
        self.command, self.args, self.env, self.cwd = command, args, env, cwd
        self._proc: asyncio.subprocess.Process | None = None
        self._stderr: collections.deque[str] = collections.deque(maxlen=15)
        self._tasks: list[asyncio.Task[None]] = []
        self._dead = False

    async def start(self) -> None:
        exe = shutil.which(self.command) or self.command  # finds npx.cmd / uvx.exe on Windows
        env = {**os.environ, **{str(k): _expand(v) for k, v in self.env.items()}}
        extra: dict[str, Any] = ({"creationflags": subprocess.CREATE_NO_WINDOW} if sys.platform == "win32"
                                 else {"start_new_session": True})
        try:
            self._proc = await asyncio.create_subprocess_exec(
                exe, *(_expand(a) for a in self.args), cwd=self.cwd, env=env,
                stdin=asyncio.subprocess.PIPE, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE,
                limit=64 * 1024 * 1024, **extra)
        except OSError as e:
            raise McpError(f"could not start {self.command}: {e}") from None
        self._tasks = [asyncio.create_task(self._read_stdout()), asyncio.create_task(self._read_stderr())]

    def detail(self) -> str:
        tail = " | ".join(list(self._stderr)[-3:])
        code = self._proc.returncode if self._proc else None
        parts = [f"exit code {code}"] if code is not None else []
        if tail:
            parts.append(tail[-400:])
        return f" ({'; '.join(parts)})" if parts else ""

    async def _read_stdout(self) -> None:
        assert self._proc is not None and self._proc.stdout is not None
        try:
            while line := await self._proc.stdout.readline():
                try:
                    msg = json.loads(line)
                except ValueError:  # logs or a traceback printed to stdout: keep it for error messages
                    text = line.decode("utf-8", errors="replace").strip()
                    if text:
                        self._stderr.append(f"stdout: {text}")
                    continue
                await self._dispatch(msg)
        except (ValueError, OSError) as e:  # a line over the limit, a broken pipe
            self._stderr.append(f"stdout: {e}")
        finally:
            self._dead = True
            if self._proc is not None:
                try:
                    await asyncio.wait_for(self._proc.wait(), 1)
                except asyncio.TimeoutError:
                    pass
            self._fail_all(McpError(f"the server stopped{self.detail()}"))

    async def _read_stderr(self) -> None:
        assert self._proc is not None and self._proc.stderr is not None
        while line := await self._proc.stderr.readline():
            text = line.decode("utf-8", errors="replace").strip()
            if text:
                self._stderr.append(text)

    async def _send(self, msg: dict[str, Any]) -> None:
        proc = self._proc
        if proc is None or proc.stdin is None or proc.returncode is not None or self._dead:
            raise McpError(f"the server is not running{self.detail()}")
        proc.stdin.write(json.dumps(msg, ensure_ascii=False).encode("utf-8") + b"\n")
        try:
            await proc.stdin.drain()
        except (ConnectionError, OSError) as e:
            raise McpError(f"the server closed its input: {e}{self.detail()}") from None

    async def close(self) -> None:
        proc, self._proc = self._proc, None
        if proc is not None and proc.returncode is None:
            if proc.stdin is not None:
                try:
                    proc.stdin.close()  # a well-behaved server exits on EOF
                except OSError:
                    pass
            try:
                await asyncio.wait_for(proc.wait(), 2)
            except asyncio.TimeoutError:
                _kill_tree(proc)
                try:
                    await asyncio.wait_for(proc.wait(), 3)
                except asyncio.TimeoutError:
                    pass
        for t in self._tasks:
            t.cancel()
        self._tasks = []


async def _sse_events(r: httpx.Response) -> AsyncIterator[str]:
    data: list[str] = []
    async for line in r.aiter_lines():
        if line == "":
            if data:
                yield "\n".join(data)
                data = []
        elif line.startswith("data:"):
            value = line[5:]
            data.append(value[1:] if value.startswith(" ") else value)
    if data:
        yield "\n".join(data)


class HttpConnection(Connection):
    """A remote server over Streamable HTTP: POST each message, the answer is JSON or an SSE stream."""

    def __init__(self, url: str, headers: dict[str, Any], client: httpx.AsyncClient | None = None) -> None:
        super().__init__()
        self.url = _expand(url)
        self.headers = {str(k): _expand(v) for k, v in headers.items()}
        self._client = client or httpx.AsyncClient(timeout=httpx.Timeout(CALL_TIMEOUT, connect=15),
                                                   follow_redirects=True)
        self.session_id = ""
        self.protocol = ""  # set after initialize; sent with every later request

    def _request_headers(self) -> dict[str, str]:
        h = {"Accept": "application/json, text/event-stream", "Content-Type": "application/json", **self.headers}
        if self.session_id:
            h["Mcp-Session-Id"] = self.session_id
        if self.protocol:
            h["MCP-Protocol-Version"] = self.protocol
        return h

    async def _send(self, msg: dict[str, Any]) -> None:
        rid = msg.get("id") if "method" in msg else None
        try:
            async with self._client.stream("POST", self.url, json=msg, headers=self._request_headers()) as r:
                if sid := r.headers.get("mcp-session-id"):
                    self.session_id = sid
                if r.status_code in (202, 204):
                    if rid is not None:  # the answer would come on a GET stream we don't open: fail now
                        raise McpError(f"HTTP {r.status_code} without an answer: this server sends answers "
                                       "only on a separate stream, which ANIX Code does not support yet")
                    return
                if r.status_code >= 400:
                    body = (await r.aread()).decode("utf-8", errors="replace").strip()
                    hint = " (check the token in \"headers\")" if r.status_code in (401, 403) else ""
                    raise McpError(f"HTTP {r.status_code}{hint}: {body[:300]}")
                if "text/event-stream" in r.headers.get("content-type", ""):
                    async for data in _sse_events(r):
                        try:
                            parsed = json.loads(data)
                        except ValueError:
                            continue
                        await self._dispatch(parsed)
                        fut = self._pending.get(rid) if rid is not None else None
                        if fut is not None and fut.done():
                            break  # got our answer; don't wait for the server to close the stream
                else:
                    body = await r.aread()
                    if body.strip():
                        try:
                            parsed = json.loads(body)
                        except ValueError:
                            raise McpError(f"the server did not answer with JSON: {body[:200]!r}") from None
                        await self._dispatch(parsed)
        except httpx.HTTPError as e:
            raise McpError(f"{type(e).__name__}: {e}") from None

    async def close(self) -> None:
        if self.session_id:
            try:
                await self._client.delete(self.url, headers=self._request_headers(), timeout=5)
            except httpx.HTTPError:
                pass
        await self._client.aclose()


# -- servers and tools ---------------------------------------------------------------------
class McpServer:
    def __init__(self, name: str, conn: Connection, timeout: float = CALL_TIMEOUT,
                 allow: set[str] | None = None) -> None:
        self.name, self.conn, self.timeout, self.allow = name, conn, timeout, allow
        self.tools: list[dict[str, Any]] = []
        self.info: dict[str, Any] = {}
        self.instructions = ""

    async def connect(self) -> None:
        await self.conn.start()
        init: dict[str, Any] = {}
        for i, version in enumerate(SUPPORTED_VERSIONS):
            try:
                init = await self.conn.request("initialize", {
                    "protocolVersion": version, "capabilities": {},
                    "clientInfo": {"name": "anixcode", "title": "ANIX Code", "version": __version__},
                }, START_TIMEOUT)
                break
            except McpError as e:
                if "version" not in str(e).lower() or i == len(SUPPORTED_VERSIONS) - 1:
                    raise
        if isinstance(self.conn, HttpConnection):
            self.conn.protocol = str(init.get("protocolVersion") or PROTOCOL_VERSION)
        info = init.get("serverInfo")
        self.info = info if isinstance(info, dict) else {}
        self.instructions = str(init.get("instructions") or "")
        await self.conn.notify("notifications/initialized")
        cursor = None
        for _ in range(100):
            page = await self.conn.request("tools/list", {"cursor": cursor} if cursor else {}, START_TIMEOUT)
            self.tools += [t for t in page.get("tools") or [] if isinstance(t, dict) and isinstance(t.get("name"), str)
                           and (self.allow is None or t["name"] in self.allow)]
            cursor = page.get("nextCursor")
            if not cursor:
                break

    async def call(self, tool: str, args: dict[str, Any]) -> dict[str, Any]:
        return await self.conn.request("tools/call", {"name": tool, "arguments": args}, self.timeout)


def make_server(name: str, cfg: dict[str, Any], root: Path) -> McpServer:
    timeout = float(cfg.get("timeout") or CALL_TIMEOUT)
    allow = set(map(str, cfg["tools"])) if isinstance(cfg.get("tools"), list) else None
    if cfg.get("type") == "sse":
        raise McpError("the old SSE transport is not supported: use the server's Streamable HTTP address "
                       "(usually ends with /mcp)")
    if cfg.get("url"):
        conn: Connection = HttpConnection(str(cfg["url"]), dict(cfg.get("headers") or {}))
    elif cfg.get("command"):
        args = cfg.get("args") or []
        if not isinstance(args, list):
            raise McpError('"args" must be a list')
        command = os.path.expanduser(_expand(cfg["command"]))  # "~/bin/server", "${HOME}/bin/server"
        conn = StdioConnection(command, [str(a) for a in args], dict(cfg.get("env") or {}), root)
    else:
        raise McpError('needs "command" (a local server) or "url" (a remote one)')
    return McpServer(name, conn, timeout, allow)


def tool_name(server: str, tool: str, taken: dict[str, Any] | set[str]) -> str:
    base = _UNSAFE.sub("_", f"mcp__{server}__{tool}")[:64]
    name, n = base, 2
    while name in taken:
        suffix = f"_{n}"
        name = base[: 64 - len(suffix)] + suffix
        n += 1
    return name


def render_result(result: dict[str, Any]) -> str:
    parts: list[str] = []
    for c in result.get("content") or []:
        if not isinstance(c, dict):
            continue
        kind = c.get("type")
        if kind == "text":
            parts.append(str(c.get("text", "")))
        elif kind in ("image", "audio"):
            parts.append(f"[{kind} {c.get('mimeType', '')}: not shown]")
        elif kind == "resource":
            res = c.get("resource") or {}
            parts.append(str(res.get("text") or f"[resource {res.get('uri', '')}]"))
        elif kind == "resource_link":
            parts.append(f"[link] {c.get('name') or ''} {c.get('uri', '')}".strip())
        else:
            parts.append(json.dumps(c, ensure_ascii=False))
    if not parts and result.get("structuredContent") is not None:
        parts.append(json.dumps(result["structuredContent"], ensure_ascii=False, indent=2))
    return "\n\n".join(parts) or "(no output)"


class McpTool(Tool):
    """One tool of an MCP server, shown to the model as mcp__<server>__<tool>."""

    kind = "mcp"  # its own permission: settings.json "permissions": {"mcp": "ask" | "allow" | "deny"}

    def __init__(self, server: McpServer, spec: dict[str, Any], name: str) -> None:
        self.server = server
        self.remote = str(spec["name"])
        self.name = name  # type: ignore[misc]
        desc = " ".join(str(spec.get("description") or spec.get("title") or self.remote).split())
        self.description = f"[MCP · {server.name}] {desc[:1000]}"  # type: ignore[misc]
        schema = spec.get("inputSchema")
        if not isinstance(schema, dict) or schema.get("type", "object") != "object":
            schema = {"type": "object", "properties": {}}
        schema = {k: v for k, v in schema.items() if k != "$schema"}
        schema.setdefault("type", "object")  # some servers leave it out and give only "properties"
        schema.setdefault("properties", {})
        self.parameters = schema  # type: ignore[misc]

    def title(self, args: dict[str, Any]) -> str:
        first = next((str(v) for v in args.values() if isinstance(v, (str, int, float))), "")
        first = " ".join(first.split())
        return f"{self.server.name} · {self.remote}" + (f"  {first[:60]}" if first else "")

    def preview(self, args: dict[str, Any], ctx: ToolContext) -> str | None:
        return truncate(json.dumps(args, ensure_ascii=False, indent=2), 4000)

    async def run(self, args: dict[str, Any], ctx: ToolContext) -> ToolResult:
        try:
            result = await self.server.call(self.remote, args)
        except McpError as e:
            raise ToolError(f"MCP server {self.server.name}: {e}") from None
        return ToolResult(truncate(render_result(result)), error=bool(result.get("isError")))


class McpManager:
    """Starts every server from settings, collects their tools, closes them on exit."""

    def __init__(self, config: dict[str, Any] | None, root: Path) -> None:
        self.config = config or {}
        self.root = root
        self.servers: list[McpServer] = []
        self.errors: dict[str, str] = {}

    async def start(self) -> None:
        async def one(name: str, cfg: dict[str, Any]) -> McpServer | None:
            server: McpServer | None = None
            try:
                server = make_server(name, cfg, self.root)
                await server.connect()
                return server
            except asyncio.CancelledError:
                if server is not None:
                    await server.conn.close()
                raise
            except Exception as e:  # one broken server must not break the app
                self.errors[name] = str(e) or type(e).__name__
                if server is not None:
                    await server.conn.close()
                return None

        jobs = [one(str(n), c) for n, c in self.config.items() if isinstance(c, dict) and not c.get("disabled")]
        self.servers = [s for s in await asyncio.gather(*jobs) if s is not None]

    def tools(self) -> dict[str, Tool]:
        out: dict[str, Tool] = {}
        for s in self.servers:
            for spec in s.tools:
                name = tool_name(s.name, spec["name"], out)
                out[name] = McpTool(s, spec, name)
        return out

    def summary(self) -> list[tuple[str, bool, str]]:
        """(server, ok, details) for /status and /doctor."""
        rows = [(s.name, True, f"{len(s.tools)} tools") for s in self.servers]
        return rows + [(n, False, e) for n, e in self.errors.items()]

    async def close(self) -> None:
        await asyncio.gather(*(s.conn.close() for s in self.servers), return_exceptions=True)
        self.servers = []


# -- the /mcp command ------------------------------------------------------------------------
ADD_USAGE = "/mcp add <name> [KEY=value …] <command …> | <https://url> [\"Header: value\" …]"


def _split(text: str) -> list[str]:
    if sys.platform != "win32":
        return shlex.split(text)
    words = shlex.split(text, posix=False)  # keep backslashes in C:\paths
    return [w[1:-1] if len(w) >= 2 and w[0] == w[-1] and w[0] in "\"'" else w for w in words]


def parse_add(text: str) -> tuple[str, dict[str, Any]]:
    """'/mcp add' arguments -> (name, server config).

    gh GITHUB_PERSONAL_ACCESS_TOKEN=${GITHUB_TOKEN} npx -y @modelcontextprotocol/server-github
    docs https://example.com/mcp "Authorization: Bearer ${DOCS_TOKEN}"
    """
    try:
        words = _split(text)
    except ValueError as e:  # an unclosed quote
        raise McpError(tr("Не разобрать команду: {0}", e)) from None
    if len(words) < 2:
        raise McpError(tr("Формат: {0}", ADD_USAGE))
    name, rest = words[0], words[1:]
    if not _SERVER_NAME.match(name):
        raise McpError(tr("Имя сервера: латиница, цифры, - и _ (например github), а не «{0}»", name))
    env: dict[str, str] = {}
    while rest and _ENV_PAIR.match(rest[0]):
        key, _, value = rest.pop(0).partition("=")
        env[key] = value
    if not rest:
        raise McpError(tr("Не указана команда или адрес сервера. Формат: {0}", ADD_USAGE))
    if re.match(r"^https?://", rest[0], re.IGNORECASE):
        if env:
            raise McpError(tr("KEY=value работает только для локальных серверов; для адреса используйте "
                              "заголовки: \"Authorization: Bearer ${TOKEN}\""))
        cfg: dict[str, Any] = {"url": rest[0]}
        headers = {}
        for h in rest[1:]:
            key, sep, value = h.partition(":")
            if not sep or not key.strip():
                raise McpError(tr("Заголовок должен выглядеть как \"Имя: значение\", а не «{0}»", h))
            headers[key.strip()] = value.strip()
        if headers:
            cfg["headers"] = headers
        return name, cfg
    cfg = {"command": rest[0], "args": rest[1:]}
    if env:
        cfg["env"] = env
    return name, cfg


def describe(cfg: dict[str, Any], limit: int = 60) -> str:
    """Short 'where is it' line: the address or the command."""
    text = str(cfg.get("url") or " ".join([str(cfg.get("command", "")), *map(str, cfg.get("args") or [])]))
    return text if len(text) <= limit else text[: limit - 1] + "…"


def status_rows(config: dict[str, Any], manager: McpManager | None) -> list[tuple[str, str, str, str]]:
    """(name, state, info, where) for every configured server.

    state: ok (info = number of tools) | error (info = the error) | off | starting.
    """
    connected = {s.name: s for s in manager.servers} if manager else {}
    errors = manager.errors if manager else {}
    rows = []
    for name, cfg in config.items():
        if not isinstance(cfg, dict):
            continue
        where = describe(cfg)
        if cfg.get("disabled"):
            rows.append((name, "off", "", where))
        elif name in connected:
            rows.append((name, "ok", str(len(connected[name].tools)), where))
        elif name in errors:
            rows.append((name, "error", errors[name], where))
        else:
            rows.append((name, "starting", "", where))
    return rows
