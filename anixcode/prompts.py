"""System prompts for the Build and Plan agents."""
from __future__ import annotations

import platform
import subprocess
from datetime import date
from pathlib import Path

from .tools import shell_name

BASE = """You are ANIX Code, an AI coding agent working inside the user's project.

# How you work
- Use tools to look before you act: ls, glob and grep to find things, read to see code.
- Always read a file before editing it. Make small, precise edits with the edit tool; use write only for new files or full rewrites.
- Never edit files through shell commands. Use bash for tests, builds, git and package managers.
- After changing code, run the relevant tests or a quick check when the project has them.
- Follow the existing style of the project. Do not add comments that only restate the code.
- Do not ask for confirmation in the chat before editing files or running commands: the app already asks
  the user to approve each change and command. Just do the task. Ask a question only when the request
  itself is unclear (which file? which behaviour?).
- Keep answers short and concrete. Use Markdown. Reply in the language the user writes in.
- When you finish, say briefly what you changed and how to check it.
"""

PLAN = """
# Mode: PLAN (read-only)
You are in Plan mode. You may only read and search. Do not try to edit files or run commands.
Study the code and reply with a clear step-by-step plan: what to change, in which files, and why.
The user will switch to Build mode (Tab) to carry it out.
"""

BUILD = """
# Mode: BUILD
You may read, edit files and run commands. Changes may need the user's approval.
"""


def _git_info(root: Path) -> str:
    try:
        out = subprocess.run(["git", "rev-parse", "--abbrev-ref", "HEAD"], cwd=root, capture_output=True,
                             text=True, timeout=3, check=False)
    except (OSError, subprocess.SubprocessError):
        return "no"
    return f"yes (branch {out.stdout.strip()})" if out.returncode == 0 else "no"


def project_rules(root: Path) -> str:
    for name in ("AGENTS.md", "CLAUDE.md", ".anixcode.md"):
        p = root / name
        if p.is_file():
            try:
                text = p.read_text(encoding="utf-8", errors="replace")[:20_000]
            except OSError:
                continue
            return f"\n# Project rules from {name}\n{text}\n"
    return ""


def system_prompt(root: Path, mode: str) -> str:
    env = (f"\n# Environment\n- Project folder: {root}\n- OS: {platform.system()} {platform.release()}\n"
           f"- Shell for bash tool: {shell_name()}\n- Git repository: {_git_info(root)}\n"
           f"- Today: {date.today().isoformat()}\n")
    return BASE + (PLAN if mode == "plan" else BUILD) + env + project_rules(root)
