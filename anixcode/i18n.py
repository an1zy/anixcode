"""Interface language: Russian (source strings) or English.

The language is chosen once at start-up: settings.json "language" ("ru" / "en"), else the
ANIXCODE_LANG environment variable, else the system language (Russian-speaking systems get Russian,
everyone else English). Changing it needs a restart.
"""
from __future__ import annotations

import json
import locale
import os
import sys
from pathlib import Path

LANGUAGES = {"ru": "Русский", "en": "English"}
_RU_FAMILY = {"ru", "uk", "be", "kk"}


def _config_dir() -> Path:  # same rules as settings.config_dir(), without importing it (no cycles)
    if env := os.environ.get("ANIXCODE_HOME"):
        return Path(env)
    if sys.platform == "win32":
        return Path(os.environ.get("APPDATA") or Path.home() / "AppData" / "Roaming") / "anixcode"
    return Path(os.environ.get("XDG_CONFIG_HOME") or Path.home() / ".config") / "anixcode"


def system_language() -> str:
    code = ""
    if sys.platform == "win32":
        try:
            import ctypes
            from locale import windows_locale

            code = windows_locale.get(ctypes.windll.kernel32.GetUserDefaultUILanguage(), "")
        except (AttributeError, OSError, ImportError):
            code = ""
    if not code:
        code = os.environ.get("LC_ALL") or os.environ.get("LC_MESSAGES") or os.environ.get("LANG") or ""
    if not code:
        try:
            code = locale.getlocale()[0] or ""
        except ValueError:
            code = ""
    return "ru" if code[:2].lower() in _RU_FAMILY else "en"


def detect_language() -> str:
    try:
        saved = json.loads((_config_dir() / "settings.json").read_text(encoding="utf-8")).get("language", "")
    except (OSError, ValueError, AttributeError):
        saved = ""
    for candidate in (saved, os.environ.get("ANIXCODE_LANG", "")):
        if candidate in LANGUAGES:
            return candidate
    return system_language()


LANG = detect_language()


def tr(text: str, *args: object) -> str:
    """Translate a Russian source string; positional args fill {0}, {1}, …"""
    if LANG == "en":
        from .i18n_en import EN

        text = EN.get(text, text)
    return text.format(*args) if args else text
