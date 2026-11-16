"""Optional: show agent activity in Discord through AI-RPC (https://github.com/an1zy/ai-rpc).

Silently does nothing when AI-RPC is not installed or not running.
"""
from __future__ import annotations

import json
import os
import sys
import threading
import tomllib
import urllib.request
from pathlib import Path
from typing import Any


def _airpc_dir() -> Path:
    if sys.platform == "win32":
        return Path(os.environ.get("APPDATA") or Path.home() / "AppData" / "Roaming") / "ai-rpc"
    return Path(os.environ.get("XDG_CONFIG_HOME") or Path.home() / ".config") / "ai-rpc"


class AirpcReporter:
    def __init__(self, enabled: bool = True, client: str | None = None) -> None:
        self.client = client or f"anixcode:{os.getpid()}"
        self._url: str | None = None
        self._token: str | None = None
        if enabled:
            base = _airpc_dir()
            try:
                self._token = (base / "server.token").read_text(encoding="utf-8").strip() or None
            except OSError:
                self._token = None
            port = 8765
            try:
                port = int(tomllib.loads((base / "config.toml").read_text(encoding="utf-8"))["server"]["port"])
            except (OSError, KeyError, ValueError, TypeError, tomllib.TOMLDecodeError):
                pass
            self._url = f"http://127.0.0.1:{port}/event"

    @property
    def active(self) -> bool:
        return bool(self._token and self._url)

    def send(self, event: str, **fields: Any) -> None:
        if not self.active:
            return
        body = json.dumps({"client": self.client, "event": event, "agent": "anix-code",
                           **{k: v for k, v in fields.items() if v is not None}}).encode()
        req = urllib.request.Request(self._url, data=body, method="POST", headers={  # type: ignore[arg-type]
            "Authorization": f"Bearer {self._token}", "Content-Type": "application/json"})

        def post() -> None:
            try:
                urllib.request.urlopen(req, timeout=1.5).close()
            except OSError:
                pass

        threading.Thread(target=post, daemon=True).start()
