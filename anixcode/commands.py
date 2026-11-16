"""Slash commands: built-in actions, ready-made prompt commands, and your own from Markdown files.

Own commands (like Claude Code / OpenCode): put a Markdown file into
  <project>/.anixcode/commands/<name>.md   (only this project)
  <config dir>/commands/<name>.md          (every project)
Optional front matter:  ---\\ndescription: what it does\\n---
In the text, $ARGUMENTS is replaced with everything after the command, $1..$9 with single words.
"""
from __future__ import annotations
from .i18n import tr

import re
import shlex
from dataclasses import dataclass
from pathlib import Path

from .tools.base import read_text

NAME = re.compile(r"^[a-z0-9][a-z0-9_-]{0,40}$")


@dataclass(frozen=True)
class Command:
    name: str
    description: str
    action: str | None = None  # handled by the app
    template: str | None = None  # sent to the agent
    args_hint: str = ""
    source: str = "builtin"  # builtin | project | user

    @property
    def takes_args(self) -> bool:
        return bool(self.args_hint)


INIT_PROMPT = """Study this project and write an AGENTS.md file in its root folder: instructions for AI coding
agents working here. Include: what the project is; a short map of the folder structure; exact commands to
install dependencies, run, build and test; code style and conventions you actually observe in the code;
anything risky or important (generated files, secrets, slow tests). Be concise and practical (under 120 lines).
If AGENTS.md already exists, improve it and keep the user's own rules. Write it in the language of the existing
docs (README), or in English if there are none. Start by exploring with ls, glob, grep and read."""

PROMPTS: tuple[Command, ...] = (
    Command("init", tr("изучить проект и написать AGENTS.md"), template=INIT_PROMPT),
    Command("review", tr("ревью незакоммиченных изменений: баги, риски"), args_hint=tr("[на что смотреть]"), template=(
        "Review the current uncommitted changes: run `git status`, `git diff` and `git diff --staged`. If there are "
        "no changes, review the last commit (`git show --stat` then `git show`). Look for bugs, security problems, "
        "missed edge cases, broken behaviour and unclear code. Do not edit files. Report findings ordered by "
        "severity, each with file:line and a concrete suggestion, then a one-line verdict.\nFocus: $ARGUMENTS")),
    Command("commit", tr("закоммитить изменения с хорошим сообщением"), args_hint=tr("[уточнения]"), template=(
        "Commit the current changes. Run `git status`, `git diff`, `git diff --staged` and `git log -5 --oneline`. "
        "Stage the relevant files (never secrets, .env files or build artifacts). Write a concise commit message in "
        "the style and language of the recent commits (Conventional Commits if the project uses them): a summary "
        "line, then a short body explaining why, if useful. Run `git commit`. Do not push.\nExtra: $ARGUMENTS")),
    Command("test", tr("найти и запустить тесты, починить падающие"), args_hint=tr("[что тестировать]"), template=(
        "Find out how tests are run in this project (AGENTS.md, README, package/config files) and run them. If "
        "something fails, find the cause, fix it with minimal changes and run the tests again, until they pass or "
        "you can clearly explain what blocks you.\nScope: $ARGUMENTS")),
    Command("fix", tr("починить ошибку: вставьте текст ошибки или опишите баг"), args_hint=tr("<ошибка или описание>"),
            template=("Fix this problem: $ARGUMENTS\n\nFirst locate the cause (read the code, reproduce it if you "
                      "can), then make a minimal fix and verify it (run the tests or the failing command).")),
    Command("explain", tr("объяснить код: файл, функцию или весь проект"), args_hint=tr("<@файл или что объяснить>"),
            template=("Explain $ARGUMENTS: what it does, how it works, the important parts and how it connects to the "
                      "rest of the project. Read the relevant code first. Do not edit files. Be clear and concrete.")),
    Command("pr", tr("описание pull request для текущей ветки"), args_hint=tr("[уточнения]"), template=(
        "Write a pull request description for the current branch. Find the default branch (main or master), then "
        "look at `git log <default>..HEAD --oneline` and `git diff <default>...HEAD --stat` and the diff itself. "
        "Output a title, a short summary, a list of changes and how to test them. Do not push or create anything."
        "\nExtra: $ARGUMENTS")),
)

ACTIONS: tuple[Command, ...] = (
    Command("help", tr("все команды и клавиши"), action="help"),
    Command("new", tr("новая сессия"), action="new"),
    Command("clear", tr("новая сессия (то же, что /new)"), action="new"),
    Command("sessions", tr("прошлые сессии этого проекта"), action="sessions"),
    Command("resume", tr("продолжить прошлую сессию"), action="sessions"),
    Command("undo", tr("откатить правки файлов из последнего ответа"), action="undo"),
    Command("redo", tr("вернуть то, что откатили /undo"), action="redo"),
    Command("compact", tr("сжать историю, чтобы освободить контекст"), action="compact"),
    Command("model", tr("модель, провайдер и API-ключ"), action="connect"),
    Command("models", tr("модель, провайдер и API-ключ"), action="connect"),
    Command("connect", tr("подключить провайдера"), action="connect"),
    Command("project", tr("выбрать проект или склонировать репозиторий"), action="project"),
    Command("theme", tr("выбрать тему"), action="theme"),
    Command("themes", tr("выбрать тему"), action="theme"),
    Command("mode", tr("переключить Build / Plan"), action="mode"),
    Command("permissions", tr("полный доступ вкл/выкл"), action="permissions"),
    Command("status", tr("сводка: проект, модель, токены, контекст"), action="status"),
    Command("export", tr("сохранить сессию в Markdown"), action="export"),
    Command("language", tr("язык интерфейса: русский / English"), action="language"),
    Command("doctor", tr("проверить git, ripgrep, подключение к модели"), action="doctor"),
    Command("mcp", tr("MCP-серверы: список, добавить, удалить, вкл/выкл"), action="mcp",
            args_hint=tr("[add|remove|on|off|tools|reload]")),
    Command("exit", tr("выход"), action="exit"),
    Command("quit", tr("выход"), action="exit"),
)


def _parse_file(path: Path, source: str) -> Command | None:
    name = path.stem.lower()
    if not NAME.match(name):
        return None
    try:
        text = read_text(path).replace("\r\n", "\n")  # UTF-8, UTF-8 with BOM or cp1251 (old Notepad)
    except OSError:
        return None
    description = ""
    m = re.match(r"^---\s*\n(.*?)\n---\s*\n", text, re.DOTALL)
    if m:
        for line in m.group(1).splitlines():
            key, _, value = line.partition(":")
            if key.strip().lower() == "description":
                description = value.strip().strip("\"'")
        text = text[m.end():]
    text = text.strip()
    if not text:
        return None
    if not description:
        first = next((ln.strip("# ").strip() for ln in text.splitlines() if ln.strip()), "")
        description = first[:70]
    hint = tr("[аргументы]") if "$ARGUMENTS" in text or re.search(r"\$[1-9]", text) else ""
    return Command(name, description, template=text, args_hint=hint, source=source)


def custom_commands(root: Path | None, config_dir: Path | None) -> list[Command]:
    out: dict[str, Command] = {}
    for folder, source in ((config_dir / "commands" if config_dir else None, "user"),
                           (root / ".anixcode" / "commands" if root else None, "project")):
        if folder and folder.is_dir():
            for f in sorted(folder.glob("*.md")):
                cmd = _parse_file(f, source)
                if cmd:
                    out[cmd.name] = cmd  # project commands override user ones
    return list(out.values())


def all_commands(root: Path | None = None, config_dir: Path | None = None) -> list[Command]:
    """Actions can't be overridden; your own files override the ready-made prompt commands."""
    action_names = {c.name for c in ACTIONS}
    merged: dict[str, Command] = {c.name: c for c in PROMPTS}
    for c in custom_commands(root, config_dir):
        if c.name not in action_names:
            merged[c.name] = c
    return [*ACTIONS, *sorted(merged.values(), key=lambda c: c.name)]


def find(name: str, commands: list[Command]) -> Command | None:
    return next((c for c in commands if c.name == name.lower()), None)


def parse(text: str) -> tuple[str, str]:
    """'/review src/api.py' -> ('review', 'src/api.py')"""
    body = text[1:] if text.startswith("/") else text
    name, _, args = body.strip().partition(" ")
    return name.lower(), args.strip()


def render(template: str, args: str) -> str:
    try:
        words = shlex.split(args, posix=True) if args else []
    except ValueError:
        words = args.split()
    out = template.replace("$ARGUMENTS", args)
    for i in range(1, 10):
        out = out.replace(f"${i}", words[i - 1] if i <= len(words) else "")
    if args and "$ARGUMENTS" not in template and not re.search(r"\$[1-9]", template):
        out += f"\n\n{args}"
    return re.sub(r"\n(Focus|Extra|Scope):[ \t]*$", "", out.rstrip(), flags=re.MULTILINE).rstrip()


def suggest(prefix: str, commands: list[Command], limit: int = 10) -> list[Command]:
    p = prefix.lower()
    seen, starts, contains = set(), [], []
    for c in commands:
        if c.name in seen:
            continue
        if c.name.startswith(p):
            starts.append(c)
            seen.add(c.name)
        elif p and (p in c.name or (len(p) >= 3 and p in c.description.lower())):
            contains.append(c)
            seen.add(c.name)
    return (starts + contains)[:limit]


def append_memory(root: Path, note: str) -> Path:
    """'# use pnpm, not npm' -> a line in AGENTS.md (like Claude Code's # memory)."""
    path = root / "AGENTS.md"
    note = note.strip()
    if path.exists():
        text = path.read_text(encoding="utf-8")
        if tr("## Заметки") not in text and "## Notes" not in text:
            text = text.rstrip() + tr("\n\n## Заметки\n")
        elif not text.endswith("\n"):
            text += "\n"
        text += f"- {note}\n"
    else:
        text = tr('# Правила для агента\n\n## Заметки\n- {0}\n', note)
    path.write_text(text, encoding="utf-8")
    return path


def session_markdown(title: str, model: str, messages: list[dict]) -> str:
    lines = [f"# {title}", "", tr('_Модель: {0}_', model), ""]
    for m in messages:
        role, content = m.get("role"), m.get("content") or ""
        if role == "user":
            text = str(content).split("\n\nAttached by the user:")[0]
            lines += [tr("## 🧑 Вы"), "", text, ""]
        elif role == "assistant":
            if content:
                lines += ["## ◆ ANIX Code", "", str(content), ""]
            for call in m.get("tool_calls") or []:
                fn = call.get("function", {})
                lines += [f"> 🔧 `{fn.get('name')}` `{str(fn.get('arguments', ''))[:300]}`", ""]
        elif role == "tool":
            out = str(content)
            lines += [tr("<details><summary>результат</summary>"), "", "```", out[:3000], "```", "", "</details>", ""]
    return "\n".join(lines)
