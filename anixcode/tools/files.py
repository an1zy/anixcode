"""File tools: read, write, edit, ls, glob."""
from __future__ import annotations
from ..i18n import tr

import os
from pathlib import Path
from typing import Any

import time

from .base import (
    SearchTimeout, Tool, ToolContext, ToolError, ToolResult, glob_regex, is_binary, make_diff, read_text, require,
    skip_dir, walk_files,
)

MAX_READ_LINES = 2000
MAX_LINE = 2000


class ReadTool(Tool):
    name = "read"
    description = (
        "Read a text file. Returns lines prefixed with line numbers. For big files use offset "
        "(1-based line) and limit. Always read a file before editing it."
    )
    parameters = {"type": "object", "properties": {
        "path": {"type": "string", "description": "File path relative to the project root"},
        "offset": {"type": "integer", "description": "First line to read (1-based)"},
        "limit": {"type": "integer", "description": f"Number of lines (default {MAX_READ_LINES})"},
    }, "required": ["path"]}

    def title(self, args: dict[str, Any]) -> str:
        return f"read {args.get('path', '')}"

    def run_sync(self, args: dict[str, Any], ctx: ToolContext) -> ToolResult:
        p = ctx.resolve(require(args, "path"), must_exist=True)
        if p.is_dir():
            raise ToolError(f"{ctx.rel(p)} is a folder. Use ls.")
        if p.stat().st_size > 10 * 1024 * 1024:
            raise ToolError("File is larger than 10 MB.")
        if is_binary(p):
            raise ToolError(f"{ctx.rel(p)} is a binary file.")
        lines = read_text(p).splitlines()
        offset = max(1, int(args.get("offset") or 1))
        limit = max(1, min(int(args.get("limit") or MAX_READ_LINES), MAX_READ_LINES))
        chunk = lines[offset - 1: offset - 1 + limit]
        width = len(str(offset + len(chunk)))
        body = "\n".join(
            f"{i:>{width}}\t{ln if len(ln) <= MAX_LINE else ln[:MAX_LINE] + '…'}"
            for i, ln in enumerate(chunk, start=offset)
        )
        ctx.mark_read(p)
        end = offset + len(chunk) - 1
        if not lines:
            body = "(empty file)"
        elif end < len(lines):
            body += f"\n\n(lines {offset}-{end} of {len(lines)}; use offset={end + 1} to read more)"
        return ToolResult(body, title=tr('{0} строк', len(chunk)))


class WriteTool(Tool):
    name = "write"
    kind = "edit"
    description = (
        "Create a new file or fully overwrite an existing one. Prefer the edit tool for changes to "
        "existing files. Overwriting requires reading the file first."
    )
    parameters = {"type": "object", "properties": {
        "path": {"type": "string"}, "content": {"type": "string", "description": "Full file content"},
    }, "required": ["path", "content"]}

    def title(self, args: dict[str, Any]) -> str:
        return f"write {args.get('path', '')}"

    def _plan(self, args: dict[str, Any], ctx: ToolContext) -> tuple[Path, str, str]:
        p = ctx.resolve(require(args, "path"))
        content = args.get("content")
        if not isinstance(content, str):
            raise ToolError("Parameter 'content' is required (string).")
        if p.is_dir():
            raise ToolError(f"{ctx.rel(p)} is a folder.")
        old = ""
        if p.exists():
            ctx.check_fresh(p)
            old = read_text(p)
        return p, old, content

    def preview(self, args: dict[str, Any], ctx: ToolContext) -> str | None:
        try:
            p, old, new = self._plan(args, ctx)
        except ToolError:
            return None
        return make_diff(old, new, ctx.rel(p))[0]

    def run_sync(self, args: dict[str, Any], ctx: ToolContext) -> ToolResult:
        p, old, new = self._plan(args, ctx)
        existed = p.exists()
        ctx.about_to_write(p)
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(new, encoding="utf-8", newline="")
        ctx.mark_read(p)
        diff, added, removed = make_diff(old, new, ctx.rel(p))
        verb = "Updated" if existed else "Created"
        return ToolResult(f"{verb} {ctx.rel(p)} ({len(new.splitlines())} lines).", title=ctx.rel(p),
                          diff=diff, added=added, removed=removed)


class EditTool(Tool):
    name = "edit"
    kind = "edit"
    description = (
        "Replace an exact piece of text in a file. old_string must match the file exactly (including "
        "indentation) and be unique; include a few surrounding lines to make it unique, or set "
        "replace_all to change every occurrence. Read the file first."
    )
    parameters = {"type": "object", "properties": {
        "path": {"type": "string"},
        "old_string": {"type": "string", "description": "Exact text to replace"},
        "new_string": {"type": "string", "description": "Replacement text"},
        "replace_all": {"type": "boolean", "description": "Replace every occurrence (default false)"},
    }, "required": ["path", "old_string", "new_string"]}

    def title(self, args: dict[str, Any]) -> str:
        return f"edit {args.get('path', '')}"

    def _plan(self, args: dict[str, Any], ctx: ToolContext) -> tuple[Path, str, str]:
        p = ctx.resolve(require(args, "path"), must_exist=True)
        old_s, new_s = require(args, "old_string"), require(args, "new_string")
        if old_s == new_s:
            raise ToolError("old_string and new_string are identical.")
        ctx.check_fresh(p)
        text = read_text(p)
        if old_s not in text and "\r\n" in text:  # model wrote \n, file uses CRLF
            old_s, new_s = old_s.replace("\n", "\r\n"), new_s.replace("\n", "\r\n")
        count = text.count(old_s)
        if count == 0:
            raise ToolError("old_string was not found. Re-read the file and copy the text exactly.")
        if count > 1 and not args.get("replace_all"):
            raise ToolError(f"old_string was found {count} times. Add surrounding lines to make it unique "
                            "or set replace_all=true.")
        new_text = text.replace(old_s, new_s) if args.get("replace_all") else text.replace(old_s, new_s, 1)
        return p, text, new_text

    def preview(self, args: dict[str, Any], ctx: ToolContext) -> str | None:
        try:
            p, old, new = self._plan(args, ctx)
        except ToolError:
            return None
        return make_diff(old, new, ctx.rel(p))[0]

    def run_sync(self, args: dict[str, Any], ctx: ToolContext) -> ToolResult:
        p, old, new = self._plan(args, ctx)
        ctx.about_to_write(p)
        p.write_text(new, encoding="utf-8", newline="")
        ctx.mark_read(p)
        diff, added, removed = make_diff(old, new, ctx.rel(p))
        return ToolResult(f"Edited {ctx.rel(p)} (+{added} -{removed}).", title=ctx.rel(p), diff=diff,
                          added=added, removed=removed)


class LsTool(Tool):
    name = "ls"
    description = "List a folder as a tree (ignores .git, node_modules, virtualenvs, build folders)."
    parameters = {"type": "object", "properties": {
        "path": {"type": "string", "description": "Folder (default: project root)"},
        "depth": {"type": "integer", "description": "How deep to go (default 2, max 5)"},
    }}
    LIMIT = 400

    def title(self, args: dict[str, Any]) -> str:
        return f"ls {args.get('path') or '.'}"

    def run_sync(self, args: dict[str, Any], ctx: ToolContext) -> ToolResult:
        base = ctx.resolve(args.get("path"), must_exist=True)
        if not base.is_dir():
            raise ToolError(f"{ctx.rel(base)} is not a folder.")
        depth = max(1, min(int(args.get("depth") or 2), 5))
        lines: list[str] = [f"{ctx.rel(base)}/"]
        count = 0

        def walk(folder: Path, level: int) -> None:
            nonlocal count
            try:
                entries = sorted(folder.iterdir(), key=lambda e: (not e.is_dir(), e.name.lower()))
            except OSError:
                return
            for e in entries:
                if count >= self.LIMIT or (e.is_dir() and skip_dir(e.name, str(e))):
                    continue
                count += 1
                lines.append(f"{'  ' * level}{e.name}{'/' if e.is_dir() else ''}")
                if e.is_dir() and level < depth:
                    walk(e, level + 1)

        walk(base, 1)
        if count >= self.LIMIT:
            lines.append(f"… (stopped at {self.LIMIT} entries)")
        return ToolResult("\n".join(lines), title=tr('{0} элементов', count))


class GlobTool(Tool):
    name = "glob"
    description = ("Find files by glob pattern, newest first. 'src/**/*.ts' matches paths; a pattern without '/' "
                   "like '*.py' or 'calc.py' matches file names in all subfolders.")
    parameters = {"type": "object", "properties": {
        "pattern": {"type": "string"}, "path": {"type": "string", "description": "Folder to search in"},
    }, "required": ["pattern"]}
    LIMIT = 100
    TIME_LIMIT = 10.0

    def title(self, args: dict[str, Any]) -> str:
        return f"glob {args.get('pattern', '')}"

    def run_sync(self, args: dict[str, Any], ctx: ToolContext) -> ToolResult:
        pattern = require(args, "pattern").replace("\\", "/")
        base = ctx.resolve(args.get("path"), must_exist=True)
        regex = glob_regex(pattern)
        by_name = "/" not in pattern.strip("/")
        found: list[Path] = []
        timed_out = False
        try:
            for p in walk_files(base, deadline=time.monotonic() + self.TIME_LIMIT):
                target = p.name if by_name else p.relative_to(base).as_posix()
                if regex.fullmatch(target):
                    found.append(p)
                    if len(found) >= 5000:
                        break
        except SearchTimeout:
            timed_out = True

        def mtime(p: Path) -> float:
            try:
                return os.stat(p).st_mtime
            except OSError:
                return 0.0

        found.sort(key=mtime, reverse=True)
        shown = [ctx.rel(p) for p in found[: self.LIMIT]]
        out = "\n".join(shown) if shown else "No files found."
        if len(found) > self.LIMIT:
            out += f"\n… and {len(found) - self.LIMIT} more"
        if timed_out:
            out += (f"\n(search stopped after {self.TIME_LIMIT:.0f}s: the folder is huge. "
                    "Pass a narrower path, or ask the user to open the right project folder.)")
        return ToolResult(out, title=tr('{0} файлов', len(found)) + (tr(" · стоп по времени") if timed_out else ""))


__all__ = ["ReadTool", "WriteTool", "EditTool", "LsTool", "GlobTool", "walk_files"]
