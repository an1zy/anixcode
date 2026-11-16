import ast
import os
import string
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent


def _tr_keys() -> set[str]:
    keys = set()
    for path in (ROOT / "anixcode").rglob("*.py"):
        for node in ast.walk(ast.parse(path.read_text(encoding="utf-8"))):
            if isinstance(node, ast.Call) and getattr(node.func, "id", "") == "tr" and node.args:
                if isinstance(node.args[0], ast.Constant):
                    keys.add(node.args[0].value)
    return keys


def _fields(s: str) -> list:
    return sorted((f, spec) for _, f, spec, _ in string.Formatter().parse(s) if f is not None)


def test_every_string_has_an_english_translation():
    from anixcode.i18n_en import EN

    missing = sorted(k for k in _tr_keys() if k not in EN)
    assert not missing, f"add to i18n_en.py: {missing[:5]}"
    broken = [k for k, v in EN.items() if _fields(k) != _fields(v)]
    assert not broken, f"placeholders differ: {broken[:3]}"


def test_english_mode_works():
    code = ("from anixcode import commands, providers; from anixcode.i18n import tr, LANG; "
            "print(LANG, commands.find('review', commands.all_commands()).description, "
            "providers.get('ollama').name, tr('Сохранено: {0} · {1}', 'a', 'b'))")
    env = {**os.environ, "ANIXCODE_LANG": "en", "PYTHONIOENCODING": "utf-8"}
    out = subprocess.run([sys.executable, "-c", code], cwd=ROOT, env=env, capture_output=True, text=True,
                         encoding="utf-8").stdout.strip()
    assert out == "en review uncommitted changes: bugs, risks Ollama (local) Saved: a · b", out
