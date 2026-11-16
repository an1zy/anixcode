"""Tool interface and the context tools run in."""
from __future__ import annotations

import asyncio
import difflib
import os
import re
import sys
import time
from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, ClassVar, Literal

Kind = Literal["read", "edit", "exec"]

IGNORE_DIRS = frozenset({
    ".git", "node_modules", "__pycache__", ".venv", "venv", ".mypy_cache", ".pytest_cache", ".ruff_cache",
    "dist", "build", ".next", ".idea", ".vscode", "target", ".tox", ".cache", ".gradle",
})


# Never walked into: Windows profile junk and junction loops ("Application Data" points back into AppData).
WALK_SKIP = frozenset(n.lower() for n in (
    "AppData", "Application Data", "Local Settings", "Cookies", "NetHood", "PrintHood", "Recent", "SendTo",
    "Start Menu", "Templates", "My Documents", "$Recycle.Bin", "System Volume Information", "Library",
    "Windows", "Program Files", "Program Files (x86)", "ProgramData",
))


class SearchTimeout(Exception):
    """A file walk took too long (searching a whole disk or home folder)."""


def skip_dir(name: str, path: str = "") -> bool:
    if name in IGNORE_DIRS or name.endswith(".egg-info") or name.lower() in WALK_SKIP:
        return True
    isjunction = getattr(os.path, "isjunction", None)
    return bool(path and isjunction and isjunction(path))


def glob_regex(pattern: str) -> re.Pattern[str]:
    """Glob with ** support -> regex over '/'-separated relative paths."""
    out, i = [], 0
    pattern = pattern.replace("\\", "/").lstrip("./") if pattern not in (".", "./") else "*"
    while i < len(pattern):
        if pattern.startswith("**/", i):
            out.append("(?:.*/)?")
            i += 3
        elif pattern.startswith("**", i):
            out.append(".*")
            i += 2
        elif pattern[i] == "*":
            out.append("[^/]*")
            i += 1
        elif pattern[i] == "?":
            out.append("[^/]")
            i += 1
        elif pattern[i] == "[":
            j = pattern.find("]", i)
            if j == -1:
                out.append(re.escape("["))
                i += 1
            else:
                out.append("[" + pattern[i + 1:j].replace("!", "^", 1) + "]")
                i = j + 1
        else:
            out.append(re.escape(pattern[i]))
            i += 1
    return re.compile("".join(out), re.IGNORECASE if sys.platform == "win32" else 0)


class ToolError(Exception):
    """Expected failure; the message is shown to the model so it can fix its call."""


@dataclass
class ToolResult:
    output: str  # what the model sees
    title: str = ""  # short line for the UI
    diff: str | None = None  # unified diff for file changes
    error: bool = False
    added: int = 0
    removed: int = 0


@dataclass
class ToolContext:
    root: Path
    read_files: dict[Path, float] = field(default_factory=dict)  # path -> mtime when last read
    before_write: Callable[[Path], None] | None = None  # snapshot hook used by /undo

    def about_to_write(self, p: Path) -> None:
        if self.before_write is not None:
            self.before_write(p)

    def resolve(self, raw: str | None, must_exist: bool = False) -> Path:
        if not raw or not str(raw).strip():
            raw = "."
        p = Path(os.path.expanduser(str(raw)))
        p = (p if p.is_absolute() else self.root / p).resolve()
        root = self.root.resolve()
        if p != root and root not in p.parents:
            raise ToolError(f"Path is outside the project folder: {raw}")
        if must_exist and not p.exists():
            raise ToolError(f"Not found: {self.rel(p)}")
        return p

    def rel(self, p: Path) -> str:
        try:
            return p.resolve().relative_to(self.root.resolve()).as_posix() or "."
        except ValueError:
            return str(p)

    def mark_read(self, p: Path) -> None:
        self.read_files[p] = p.stat().st_mtime

    def check_fresh(self, p: Path) -> None:
        """Edits require a prior read, and the file must not have changed since."""
        if p not in self.read_files:
            raise ToolError(f"Read {self.rel(p)} with the read tool before changing it.")
        if p.stat().st_mtime != self.read_files[p]:
            raise ToolError(f"{self.rel(p)} changed since you last read it. Read it again first.")


class Tool:
    name: ClassVar[str]
    kind: ClassVar[Kind] = "read"
    description: ClassVar[str]
    parameters: ClassVar[dict[str, Any]]

    def schema(self) -> dict[str, Any]:
        return {"type": "function", "function": {
            "name": self.name, "description": self.description, "parameters": self.parameters}}

    def title(self, args: dict[str, Any]) -> str:
        """Short description for the UI, e.g. 'read src/app.py'."""
        return self.name

    def preview(self, args: dict[str, Any], ctx: ToolContext) -> str | None:
        """What to show in the permission dialog (a diff or a command)."""
        return None

    async def run(self, args: dict[str, Any], ctx: ToolContext) -> ToolResult:
        """File tools do blocking disk work: run it in a thread so the interface never freezes."""
        return await asyncio.to_thread(self.run_sync, args, ctx)

    def run_sync(self, args: dict[str, Any], ctx: ToolContext) -> ToolResult:
        raise NotImplementedError


def require(args: dict[str, Any], key: str, kind: type = str) -> Any:
    value = args.get(key)
    if not isinstance(value, kind) or (kind is str and value == "" and key != "new_string"):
        raise ToolError(f"Parameter '{key}' is required ({kind.__name__}).")
    return value


def make_diff(old: str, new: str, path: str) -> tuple[str, int, int]:
    lines = list(difflib.unified_diff(old.splitlines(keepends=True), new.splitlines(keepends=True),
                                      fromfile=f"a/{path}", tofile=f"b/{path}", n=3))
    added = sum(1 for ln in lines if ln.startswith("+") and not ln.startswith("+++"))
    removed = sum(1 for ln in lines if ln.startswith("-") and not ln.startswith("---"))
    text = "".join(ln if ln.endswith("\n") else ln + "\n" for ln in lines)
    return text, added, removed


def truncate(text: str, limit: int = 30_000) -> str:
    if len(text) <= limit:
        return text
    head, tail = text[: limit // 2], text[-limit // 2:]
    return f"{head}\n\n… [{len(text) - limit} characters cut] …\n\n{tail}"


def is_binary(p: Path) -> bool:
    try:
        with open(p, "rb") as f:
            return b"\0" in f.read(8192)
    except OSError:
        return False


def read_text(p: Path) -> str:
    data = p.read_bytes()
    for enc in ("utf-8", "utf-8-sig", "cp1251"):
        try:
            return data.decode(enc)
        except UnicodeDecodeError:
            continue
    return data.decode("utf-8", errors="replace")


def walk_files(base: Path, deadline: float | None = None):
    """All files under base, skipping ignored folders. Raises SearchTimeout after ``deadline``."""
    if base.is_file():
        yield base
        return
    for dirpath, dirnames, filenames in os.walk(base, followlinks=False):
        if deadline is not None and time.monotonic() > deadline:
            raise SearchTimeout
        dirnames[:] = sorted(d for d in dirnames if not skip_dir(d, os.path.join(dirpath, d)))
        for name in sorted(filenames):
            yield Path(dirpath) / name
