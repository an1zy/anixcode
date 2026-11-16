"""File snapshots taken before the agent changes a file, so /undo can put things back."""
from __future__ import annotations

import time
from pathlib import Path

from .storage import Session

MAX_SNAPSHOT = 20 * 1024 * 1024  # don't snapshot giant files


class Checkpoints:
    """Per-session. Uses the SQLite store when the session has one (survives restarts), else memory."""

    def __init__(self, session: Session) -> None:
        self.session = session
        self._mem: dict[int, dict[str, tuple[bool, bytes]]] = {}
        self._last_turn = 0
        # states replaced by /undo, newest last: [(path, existed, content)] per undo
        self._redo: list[list[tuple[str, bool, bytes]]] = []

    def new_turn(self) -> int:
        self._redo.clear()  # a new change makes old redo states meaningless
        turn = max(int(time.time() * 1000), self._last_turn + 1)  # monotonic, survives compaction
        self._last_turn = turn
        return turn

    def snapshot(self, turn: int, path: Path) -> None:
        existed = path.is_file()
        if existed and path.stat().st_size > MAX_SNAPSHOT:
            return
        content = path.read_bytes() if existed else b""
        store = self.session.store
        if store is not None:
            store.add_snapshot(self.session.id, turn, str(path), existed, content)
        else:
            self._mem.setdefault(turn, {}).setdefault(str(path), (existed, content))

    def undo_last(self) -> tuple[list[Path], list[Path]] | None:
        """Restore the files of the latest turn that changed something. Returns (restored, deleted)."""
        store = self.session.store
        if store is not None:
            found = store.last_snapshot_turn(self.session.id)
            if found is None:
                return None
            turn, files = found
        else:
            if not self._mem:
                return None
            turn = max(self._mem)
            files = [(p, e, c) for p, (e, c) in self._mem[turn].items()]
        restored, deleted = [], []
        current = []
        for raw, _existed, _content in files:
            p = Path(raw)
            current.append((raw, p.is_file(), p.read_bytes() if p.is_file() else b""))
        self._redo.append(current)
        for raw, existed, content in files:
            p = Path(raw)
            if existed:
                p.parent.mkdir(parents=True, exist_ok=True)
                p.write_bytes(content)
                restored.append(p)
            elif p.exists():
                p.unlink()
                deleted.append(p)
        if store is not None:
            store.drop_snapshot_turn(self.session.id, turn)
        else:
            del self._mem[turn]
        return restored, deleted

    def redo_last(self) -> tuple[list[Path], list[Path]] | None:
        """Re-apply what the latest /undo reverted. Returns (restored, deleted)."""
        if not self._redo:
            return None
        restored, deleted = [], []
        for raw, existed, content in self._redo.pop():
            p = Path(raw)
            if existed:
                p.parent.mkdir(parents=True, exist_ok=True)
                p.write_bytes(content)
                restored.append(p)
            elif p.exists():
                p.unlink()
                deleted.append(p)
        return restored, deleted
