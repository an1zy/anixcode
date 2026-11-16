"""Settings (settings.json) and API keys (auth.json, readable only by you)."""
from __future__ import annotations

import dataclasses
import json
import os
import sys
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

APP = "anixcode"
PERMISSION_VALUES = ("ask", "allow", "deny")


def config_dir() -> Path:
    if env := os.environ.get("ANIXCODE_HOME"):
        return Path(env)
    if sys.platform == "win32":
        return Path(os.environ.get("APPDATA") or Path.home() / "AppData" / "Roaming") / APP
    return Path(os.environ.get("XDG_CONFIG_HOME") or Path.home() / ".config") / APP


@dataclass
class Settings:
    provider: str = ""
    model: str = ""
    theme: str = "anix"
    animations: bool = True
    # "ask" | "allow" | "deny" for tools that change things. Reading is always allowed.
    permissions: dict[str, str] = field(default_factory=lambda: {"edit": "ask", "exec": "ask"})
    max_steps: int = 50
    full_access: bool = False  # the agent edits and runs commands without asking
    language: str = ""  # "ru" | "en"; empty = system language (applied on restart)
    context_window: int = 0  # model context size in tokens; 0 = guess from the model name
    airpc: bool = True  # report activity to AI-RPC (Discord status) when it is running
    # Your own OpenAI-compatible providers: [{"id": "user:...", "name": ..., "base_url": ...}]
    custom_providers: list[dict[str, Any]] = field(default_factory=list)
    # MCP servers, same shape as "mcpServers" in Claude Desktop / Cursor (see anixcode/mcp.py)
    mcp_servers: dict[str, dict[str, Any]] = field(default_factory=dict)
    recent_projects: list[str] = field(default_factory=list)

    @property
    def ready(self) -> bool:
        return bool(self.provider and self.model)


def _write_private(path: Path, data: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(".tmp")
    tmp.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
    try:
        tmp.chmod(0o600)
    except OSError:
        pass
    os.replace(tmp, path)


def load_settings(base: Path | None = None) -> Settings:
    path = (base or config_dir()) / "settings.json"
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return Settings()
    known = {f.name for f in dataclasses.fields(Settings)}
    if raw.get("theme") == "tokyo-night" and "animations" not in raw:
        raw["theme"] = "anix"  # 0.1.0 saved its old default theme; switch to the new default once
    if raw.get("theme") == "opencode":
        raw["theme"] = "noir"  # renamed in 0.8
    s = Settings(**{k: v for k, v in raw.items() if k in known})
    s.permissions = {k: v for k, v in dict(s.permissions).items() if v in PERMISSION_VALUES}
    s.custom_providers = [
        d for d in (s.custom_providers if isinstance(s.custom_providers, list) else [])
        if isinstance(d, dict) and str(d.get("id", "")).startswith("user:")
        and isinstance(d.get("name"), str) and isinstance(d.get("base_url"), str)
    ]
    s.recent_projects = [p for p in (s.recent_projects if isinstance(s.recent_projects, list) else [])
                         if isinstance(p, str)]
    s.mcp_servers = {str(k): v for k, v in (s.mcp_servers.items() if isinstance(s.mcp_servers, dict) else [])
                     if isinstance(v, dict)}
    return s


def save_settings(s: Settings, base: Path | None = None) -> None:
    _write_private((base or config_dir()) / "settings.json", dataclasses.asdict(s))


@dataclass
class Credentials:
    api_key: str = ""
    base_url: str = ""  # overrides the provider default (required for "custom")


def load_auth(base: Path | None = None) -> dict[str, Credentials]:
    path = (base or config_dir()) / "auth.json"
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}
    return {k: Credentials(**{f: str(v.get(f, "")) for f in ("api_key", "base_url")})
            for k, v in raw.items() if isinstance(v, dict)}


def save_auth(auth: dict[str, Credentials], base: Path | None = None) -> None:
    _write_private((base or config_dir()) / "auth.json", {k: dataclasses.asdict(v) for k, v in auth.items()})


def mask(key: str) -> str:
    return f"{key[:4]}…{key[-4:]}" if len(key) > 12 else "•" * len(key)
