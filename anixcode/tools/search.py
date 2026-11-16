"""grep: regex search in files (uses ripgrep when installed)."""
from __future__ import annotations
from ..i18n import tr

import asyncio
import fnmatch
import re
import shutil
from typing import Any

from pathlib import Path

import time

from .base import (
    IGNORE_DIRS, WALK_SKIP, SearchTimeout, Tool, ToolContext, ToolError, ToolResult, is_binary, read_text, require,
    walk_files,
)

LIMIT = 100


class GrepTool(Tool):
    name = "grep"
    description = (
        "Search file contents with a regular expression. Returns path:line: text. Use include to filter "
        "files, e.g. '*.py'."
    )
    parameters = {"type": "object", "properties": {
        "pattern": {"type": "string", "description": "Regular expression"},
        "path": {"type": "string", "description": "Folder or file to search (default: project root)"},
        "include": {"type": "string", "description": "File name glob, e.g. '*.ts'"},
    }, "required": ["pattern"]}

    def title(self, args: dict[str, Any]) -> str:
        return f"grep {args.get('pattern', '')}"

    async def run(self, args: dict[str, Any], ctx: ToolContext) -> ToolResult:
        pattern = require(args, "pattern")
        try:
            regex = re.compile(pattern)
        except re.error as e:
            raise ToolError(f"Bad regular expression: {e}") from None
        base = ctx.resolve(args.get("path"), must_exist=True)
        include = args.get("include") or None
        rg = shutil.which("rg")
        if rg:
            matches = await self._ripgrep(rg, pattern, str(base), include, ctx)
        else:
            matches = await asyncio.to_thread(self._python, regex, base, include, ctx)
        if not matches:
            return ToolResult("No matches.", title=tr("нет совпадений"))
        out = "\n".join(matches[:LIMIT])
        if len(matches) > LIMIT:
            out += f"\n… more than {LIMIT} matches, narrow the search"
        return ToolResult(out, title=tr('{0}{1} совпадений', min(len(matches), LIMIT), '+' if len(matches) > LIMIT else ''))

    async def _ripgrep(self, rg: str, pattern: str, base: str, include: str | None, ctx: ToolContext) -> list[str]:
        cmd = [rg, "--line-number", "--no-heading", "--color", "never", "--max-columns", "300",
               "--max-count", "50", "--path-separator", "/", "-e", pattern]
        for d in sorted(IGNORE_DIRS) + sorted(WALK_SKIP):
            cmd += ["--glob", f"!{d}/"]
        if include:
            cmd += ["--glob", include]
        target = ctx.rel(Path(base))
        cmd.append(target if target != "." else ".")
        # Run from the project root so paths come back relative (no "C:" drive colons to trip over).
        proc = await asyncio.create_subprocess_exec(*cmd, cwd=ctx.root, stdout=asyncio.subprocess.PIPE,
                                                    stderr=asyncio.subprocess.DEVNULL)
        try:
            out, _ = await asyncio.wait_for(proc.communicate(), timeout=30)
        except asyncio.TimeoutError:
            proc.kill()
            return ["(search stopped after 30s: pass a narrower path)"]
        lines = []
        for line in out.decode("utf-8", errors="replace").splitlines():
            lines.append(line[2:] if line.startswith("./") else line)
            if len(lines) > LIMIT:
                break
        return lines

    @staticmethod
    def _python(regex: re.Pattern[str], base, include: str | None, ctx: ToolContext) -> list[str]:
        out: list[str] = []
        files = walk_files(base, deadline=time.monotonic() + 15)
        while True:
            try:
                p = next(files)
            except StopIteration:
                break
            except SearchTimeout:
                out.append("(search stopped after 15s: the folder is huge, pass a narrower path)")
                break
            if include and not fnmatch.fnmatch(p.name, include):
                continue
            try:
                if p.stat().st_size > 2 * 1024 * 1024 or is_binary(p):
                    continue
                text = read_text(p)
            except OSError:
                continue
            for i, line in enumerate(text.splitlines(), 1):
                if regex.search(line):
                    out.append(f"{ctx.rel(p)}:{i}: {line.strip()[:300]}")
                    if len(out) > LIMIT:
                        return out
        return out
