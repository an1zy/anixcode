"""Project folders: recent list, finding git repositories, cloning."""
from __future__ import annotations
from .i18n import tr

import os
import re
import subprocess
import sys
from pathlib import Path

from .tools.base import IGNORE_DIRS

MAX_RECENT = 15
# Never searched for repositories: system and huge folders.
SKIP_DIRS = frozenset({
    "windows", "program files", "program files (x86)", "programdata", "$recycle.bin", "recovery", "perflogs",
    "system volume information", "appdata", "library", "msocache", "$windows.~bt", "$windows.~ws", "intel",
    "amd", "nvidia", "drivers", "steamlibrary", "steamapps", "games", "xboxgames", "proc", "sys", "dev", "run",
    "snap", "boot", "usr", "lib", "lib64", "bin", "sbin", "etc", "var", "opt", "application data",
    "local settings", "my documents", "cookies", "start menu",
})
GIT_URL = re.compile(r"^(https?://|git@|ssh://)\S+?(\.git)?/?$")


def is_unsuitable(path: Path) -> bool:
    """Folders where an agent should not start by default (home, drive root, Windows system folders)."""
    p = path.resolve()
    if p == Path.home().resolve() or p.parent == p:
        return True
    parts = [x.lower() for x in p.parts]
    return sys.platform == "win32" and any(x in ("windows", "program files", "program files (x86)") for x in parts)


def is_git_repo(path: Path) -> bool:
    return (path / ".git").exists()


def remember(recent: list[str], path: Path) -> list[str]:
    """Most recent first, no duplicates (case-insensitive on Windows), at most MAX_RECENT."""
    p = str(path.resolve())
    norm = (lambda s: s.lower()) if sys.platform == "win32" else (lambda s: s)
    return [p, *[r for r in recent if norm(r) != norm(p)]][:MAX_RECENT]


def drives() -> list[Path]:
    """Disks to browse: C:, D:, … on Windows; / and mounted volumes elsewhere."""
    if sys.platform == "win32":
        try:
            import ctypes

            mask = ctypes.windll.kernel32.GetLogicalDrives()  # no access to the disk itself: never hangs
        except (OSError, AttributeError):
            mask = 0
        letters = [chr(65 + i) for i in range(26) if mask >> i & 1] or ["C"]
        return [Path(f"{letter}:\\") for letter in letters if letter not in ("A", "B")]
    out = [Path("/")]
    for base in (Path("/Volumes"), Path("/mnt"), Path("/media") / os.environ.get("USER", "")):
        try:
            out += sorted(p for p in base.iterdir() if p.is_dir())
        except OSError:
            pass
    return out


def search_roots() -> list[Path]:
    home = Path.home()
    names = ("Desktop", "Documents", "Projects", "projects", "Code", "code", "dev", "Dev", "src", "repos",
             "GitHub", "git", "source/repos", "work", "Рабочий стол", "Документы", "OneDrive/Desktop",
             "OneDrive/Documents")
    roots = [home / n for n in names if (home / n).is_dir()] or [home]
    if sys.platform == "win32":
        roots += drives()  # C:\dev, D:\projects, …: system folders are skipped (SKIP_DIRS)
    return roots


def find_repos(roots: list[Path], max_depth: int = 3, limit: int = 200) -> list[Path]:
    """Git repositories under ``roots`` (does not descend into a repo once found)."""
    found: list[Path] = []
    seen: set[str] = set()

    def walk(folder: Path, depth: int) -> None:
        if len(found) >= limit:
            return
        key = os.path.normcase(str(folder))
        if key in seen:
            return
        seen.add(key)
        if is_git_repo(folder):
            found.append(folder)
            return
        if depth >= max_depth:
            return
        try:
            entries = sorted(os.scandir(folder), key=lambda e: e.name.lower())
        except OSError:
            return
        for e in entries:
            try:
                if not e.is_dir(follow_symlinks=False):
                    continue
            except OSError:
                continue
            if e.name.startswith((".", "$")) or e.name in IGNORE_DIRS or e.name.lower() in SKIP_DIRS:
                continue
            if depth == 0 and sys.platform == "win32" and e.name.lower() == "users":
                continue  # the home folder is searched separately
            walk(Path(e.path), depth + 1)

    for root in roots:
        walk(root, 0)
    found.sort(key=lambda p: _mtime(p), reverse=True)
    return found


def _mtime(p: Path) -> float:
    try:
        return (p / ".git").stat().st_mtime
    except OSError:
        return 0.0


def repo_name(url: str) -> str:
    name = re.split(r"[/\\:]", url.rstrip("/\\"))[-1]
    return re.sub(r"\.git$", "", name) or "repo"


def clone(url: str, parent: Path) -> Path:
    """git clone ``url`` into parent/<name>. Raises RuntimeError with git's message."""
    target = parent / repo_name(url)
    if target.exists():
        raise RuntimeError(tr('Папка уже существует: {0}', target))
    parent.mkdir(parents=True, exist_ok=True)
    try:
        out = subprocess.run(["git", "clone", "--", url, str(target)], capture_output=True, text=True,
                             timeout=600, check=False)
    except FileNotFoundError:
        raise RuntimeError(tr("Git не установлен. Скачайте: https://git-scm.com")) from None
    except subprocess.TimeoutExpired:
        raise RuntimeError(tr("Клонирование заняло больше 10 минут и было остановлено.")) from None
    if out.returncode != 0:
        raise RuntimeError((out.stderr or out.stdout).strip()[-500:] or tr("git clone завершился с ошибкой"))
    return target
