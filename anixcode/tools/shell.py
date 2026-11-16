"""bash: run a shell command in the project folder (PowerShell on Windows)."""
from __future__ import annotations

import asyncio
import os
import shutil
import signal
import subprocess
import sys
from typing import Any

from .base import Tool, ToolContext, ToolError, ToolResult, require, truncate


def shell_name() -> str:
    return "PowerShell" if sys.platform == "win32" else "bash"


def _command(cmd: str) -> list[str]:
    if sys.platform == "win32":
        exe = shutil.which("pwsh") or "powershell"
        prefix = "[Console]::OutputEncoding=[System.Text.Encoding]::UTF8; $ProgressPreference='SilentlyContinue'; "
        return [exe, "-NoProfile", "-NonInteractive", "-Command", prefix + cmd]
    return [shutil.which("bash") or "/bin/sh", "-c", cmd]


async def _kill(proc: asyncio.subprocess.Process) -> None:
    if proc.returncode is not None:
        return
    try:
        if sys.platform == "win32":
            subprocess.run(["taskkill", "/F", "/T", "/PID", str(proc.pid)], capture_output=True, check=False)
        else:
            os.killpg(proc.pid, signal.SIGKILL)
    except (OSError, ProcessLookupError):
        pass
    try:
        await asyncio.wait_for(proc.wait(), timeout=5)
    except asyncio.TimeoutError:
        pass


class BashTool(Tool):
    name = "bash"
    kind = "exec"
    description = (
        f"Run a {shell_name()} command in the project folder and return its output (stdout+stderr). "
        "Use it for tests, builds, git, package managers. Do not use it to read or edit files: use the "
        "file tools. Interactive commands are not supported."
    )
    parameters = {"type": "object", "properties": {
        "command": {"type": "string"},
        "timeout": {"type": "integer", "description": "Seconds (default 120, max 600)"},
        "description": {"type": "string", "description": "5-10 words on what the command does"},
    }, "required": ["command"]}

    def title(self, args: dict[str, Any]) -> str:
        cmd = " ".join(str(args.get("command", "")).split())
        return cmd if len(cmd) <= 100 else cmd[:99] + "…"

    def preview(self, args: dict[str, Any], ctx: ToolContext) -> str | None:
        return str(args.get("command", ""))

    async def run(self, args: dict[str, Any], ctx: ToolContext) -> ToolResult:
        cmd = require(args, "command")
        timeout = max(1, min(int(args.get("timeout") or 120), 600))
        extra = {"start_new_session": True} if sys.platform != "win32" else {
            "creationflags": subprocess.CREATE_NO_WINDOW}
        try:
            proc = await asyncio.create_subprocess_exec(
                *_command(cmd), cwd=ctx.root, stdin=asyncio.subprocess.DEVNULL,
                stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.STDOUT, **extra)
        except OSError as e:
            raise ToolError(f"Could not start the shell: {e}") from None
        try:
            out, _ = await asyncio.wait_for(proc.communicate(), timeout=timeout)
        except asyncio.TimeoutError:
            await _kill(proc)
            return ToolResult(f"Command timed out after {timeout}s and was stopped.", title=f"⏱ {timeout}s", error=True)
        except asyncio.CancelledError:
            await _kill(proc)
            raise
        text = truncate(out.decode("utf-8", errors="replace").replace("\r\n", "\n"))
        code = proc.returncode
        body = (text.rstrip() or "(no output)") + f"\n\n[exit code {code}]"
        return ToolResult(body, title=f"→ {code}", error=code != 0)
