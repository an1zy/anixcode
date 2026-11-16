"""@file mentions: completion candidates and attaching mentioned files to a prompt."""
from __future__ import annotations

import os
import re
from pathlib import Path

import time

from .tools.base import IGNORE_DIRS, ToolContext, ToolError, is_binary, read_text, skip_dir

MENTION = re.compile(r"(?<![\w@])@((?:[^\s@\"']|\\ )+)")
MAX_FILE_LINES = 2000
MAX_FILE_CHARS = 80_000
MAX_TOTAL_CHARS = 250_000
MAX_INDEX = 20_000


def index_files(root: Path) -> list[str]:
    """Project files and folders (relative, '/'-separated), for completion."""
    out: list[str] = []
    deadline = time.monotonic() + 5
    for dirpath, dirnames, filenames in os.walk(root, followlinks=False):
        if time.monotonic() > deadline:
            break
        dirnames[:] = sorted(d for d in dirnames if not d.startswith(".")
                             and not skip_dir(d, os.path.join(dirpath, d)))
        rel_dir = Path(dirpath).relative_to(root).as_posix()
        prefix = "" if rel_dir == "." else rel_dir + "/"
        out.extend(prefix + d + "/" for d in dirnames)
        out.extend(prefix + f for f in sorted(filenames) if not f.startswith("."))
        if len(out) >= MAX_INDEX:
            break
    return out[:MAX_INDEX]


def _subsequence(query: str, text: str) -> bool:
    it = iter(text)
    return all(ch in it for ch in query)


def rank_paths(paths: list[str], query: str, limit: int = 8) -> list[str]:
    """Best matches first: file name starts with query > name contains > path contains > fuzzy."""
    q = query.lower().replace("\\", "/")
    if not q:
        top = [p for p in paths if "/" not in p.rstrip("/")]
        return top[:limit]
    scored: list[tuple[int, int, str]] = []
    for p in paths:
        low = p.lower()
        name = low.rstrip("/").rsplit("/", 1)[-1]
        if name.startswith(q):
            score = 0
        elif q in name:
            score = 1
        elif low.startswith(q):
            score = 2
        elif q in low:
            score = 3
        elif _subsequence(q, name):
            score = 4
        else:
            continue
        scored.append((score, len(p), p))
    scored.sort()
    return [p for _, _, p in scored[:limit]]


def find_mentions(text: str) -> list[str]:
    return [m.group(1).replace("\\ ", " ").rstrip(".,;:!?)") for m in MENTION.finditer(text)]


def expand_mentions(text: str, ctx: ToolContext) -> tuple[str, list[str]]:
    """Append the content of every @mentioned file (or a listing of every @folder) to the prompt.

    Fully attached files count as read, so the agent may edit them right away.
    Returns (text for the model, attached relative paths).
    """
    blocks: list[str] = []
    attached: list[str] = []
    total = 0
    for raw in dict.fromkeys(find_mentions(text)):
        try:
            p = ctx.resolve(raw, must_exist=True)
        except ToolError:
            continue  # not a path (an e-mail, a decorator…): leave the text alone
        rel = ctx.rel(p)
        if p.is_dir():
            entries = sorted(os.listdir(p))[:200]
            listing = "\n".join(e + ("/" if (p / e).is_dir() else "") for e in entries
                                if e not in IGNORE_DIRS)
            block = f'<folder path="{rel}">\n{listing}\n</folder>'
        else:
            if is_binary(p) or p.stat().st_size > 5 * 1024 * 1024:
                continue
            lines = read_text(p).splitlines()
            body_lines, size, cut = [], 0, False
            for i, line in enumerate(lines[:MAX_FILE_LINES], 1):
                size += len(line) + 8
                if size > MAX_FILE_CHARS:
                    cut = True
                    break
                body_lines.append(f"{i}\t{line}")
            cut = cut or len(lines) > MAX_FILE_LINES
            note = f"\n(first {len(body_lines)} of {len(lines)} lines; read the rest with the read tool)" if cut else ""
            block = f'<file path="{rel}">\n' + "\n".join(body_lines) + f"{note}\n</file>"
            if not cut:
                ctx.mark_read(p)
        if total + len(block) > MAX_TOTAL_CHARS:
            break
        total += len(block)
        blocks.append(block)
        attached.append(rel)
    if not blocks:
        return text, []
    return text + "\n\nAttached by the user:\n" + "\n\n".join(blocks), attached
