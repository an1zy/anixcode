"""Dialogs: permission, model setup, sessions, help."""
from __future__ import annotations
from ..i18n import tr

import asyncio
import sys
from datetime import datetime
from pathlib import Path

from rich.syntax import Syntax
from rich.text import Text
from textual import on, work
from textual.app import ComposeResult
from textual.containers import Horizontal, Vertical, VerticalScroll
from textual.screen import ModalScreen
from textual.widgets import (
    Button, DirectoryTree, Input, Label, Markdown, OptionList, Select, Static, TabbedContent, TabPane,
)

from .. import projects, providers
from ..llm import LLMClient, LLMError
from ..settings import Credentials, Settings, mask, save_auth, save_settings
from ..tools.base import IGNORE_DIRS


class ProviderEditScreen(ModalScreen[dict | None]):
    """Add your own OpenAI-compatible provider."""

    BINDINGS = [("escape", "cancel", tr("Отмена"))]

    def compose(self) -> ComposeResult:
        with Vertical(id="dialog"):
            yield Label(tr("Свой провайдер"), classes="dialog-title")
            yield Label(tr("Подойдёт любой сервер с OpenAI-совместимым API: свой роутер, прокси, облако."),
                        classes="hint")
            yield Label(tr("Название"))
            yield Input(placeholder=tr("например: Мой ANIX"), id="p-name")
            yield Label(tr("Адрес API (обычно заканчивается на /v1)"))
            yield Input(placeholder="http://localhost:3001/v1", id="p-url")
            yield Label(tr("API-ключ (если нужен)"))
            yield Input(password=True, placeholder=tr("оставьте пустым, если ключ не нужен"), id="p-key")
            yield Static(id="p-status", classes="hint")
            with Horizontal(classes="buttons"):
                yield Button(tr("Добавить"), id="p-save", variant="primary")
                yield Button(tr("Проверить"), id="p-test")
                yield Button(tr("Отмена"), id="p-cancel")

    def on_mount(self) -> None:
        self.query_one("#p-name", Input).focus()

    def _values(self) -> tuple[str, str, str]:
        return (self.query_one("#p-name", Input).value.strip(), self.query_one("#p-url", Input).value.strip(),
                self.query_one("#p-key", Input).value.strip())

    @on(Button.Pressed, "#p-test")
    def _test_pressed(self) -> None:
        self.test_connection()

    @work(exclusive=True)
    async def test_connection(self) -> None:
        status = self.query_one("#p-status", Static)
        _name, url, key = self._values()
        if not url.startswith(("http://", "https://")):
            status.update(Text(tr("Адрес должен начинаться с http:// или https://"), style="red"))
            return
        status.update(tr("Проверяю…"))
        client = LLMClient(url, key, timeout=20)
        try:
            models = await client.list_models()
        except LLMError as e:
            status.update(Text(str(e), style="red"))
            return
        finally:
            await client.aclose()
        status.update(Text(tr('✓ Работает, моделей: {0}', len(models)) if models else
                           tr("✓ Сервер отвечает (список моделей пуст, название модели впишете вручную)"),
                           style="green"))

    @on(Button.Pressed, "#p-save")
    def _save(self) -> None:
        name, url, key = self._values()
        status = self.query_one("#p-status", Static)
        if not name:
            status.update(Text(tr("Введите название."), style="red"))
            return
        if not url.startswith(("http://", "https://")):
            status.update(Text(tr("Адрес должен начинаться с http:// или https://"), style="red"))
            return
        self.dismiss({"name": name, "base_url": url, "api_key": key})

    @on(Button.Pressed, "#p-cancel")
    def action_cancel(self) -> None:
        self.dismiss(None)


class SetupScreen(ModalScreen[bool]):
    """Pick a provider, paste a key, choose a model."""

    BINDINGS = [("escape", "cancel", tr("Отмена"))]

    def __init__(self, settings: Settings, auth: dict[str, Credentials], base: Path | None = None) -> None:
        super().__init__()
        self.settings = settings
        self.auth = auth
        self.base = base
        self._model_ids: list[str] = []

    def _options(self) -> list[tuple[str, str]]:
        return [(("★ " if p.is_user else "") + p.name, p.id) for p in providers.all_providers(self.settings)]

    def compose(self) -> ComposeResult:
        ids = {p.id for p in providers.all_providers(self.settings)}
        current = self.settings.provider if self.settings.provider in ids else "deepseek"
        with Vertical(id="dialog"):
            yield Label(tr("Подключение модели"), classes="dialog-title")
            yield Label(tr("Провайдер"))
            with Horizontal(id="provider-row"):
                yield Select(self._options(), value=current, allow_blank=False, id="provider")
                yield Button(tr("＋ Свой"), id="add-provider")
                yield Button(tr("Удалить"), id="del-provider", variant="error")
            yield Label(tr("API-ключ"))
            yield Input(password=True, id="key")
            yield Static(id="key-hint", classes="hint")
            yield Label(tr("Адрес сервера"))
            yield Input(id="base-url")
            yield Label(tr("Модель"))
            with Horizontal(id="model-row"):
                yield Input(id="model", placeholder=tr("название модели"))
                yield Button(tr("Список моделей"), id="fetch")
            yield OptionList(id="models")
            yield Static(id="setup-error", classes="error")
            with Horizontal(classes="buttons"):
                yield Button(tr("Сохранить"), id="save", variant="primary")
                yield Button(tr("Отмена"), id="cancel")

    def on_mount(self) -> None:
        self._fill(self.provider_id)

    @property
    def provider_id(self) -> str:
        return str(self.query_one("#provider", Select).value)

    @on(Select.Changed, "#provider")
    def _provider_changed(self, event: Select.Changed) -> None:
        self._fill(str(event.value))

    def _fill(self, pid: str) -> None:
        p = providers.get(pid, self.settings)
        cred = self.auth.get(pid, Credentials())
        key = self.query_one("#key", Input)
        key.value = ""
        key.placeholder = tr('сохранён: {0}', mask(cred.api_key)) if cred.api_key else ("sk-…" if p.needs_key else tr("не обязателен"))
        hint = tr('Где взять ключ: {0}', p.key_url) if p.key_url else (tr("Ключ не нужен") if not p.needs_key else "")
        self.query_one("#key-hint", Static).update(hint)
        self.query_one("#base-url", Input).value = cred.base_url or p.base_url
        model = self.settings.model if self.settings.provider == pid else p.default_model
        self.query_one("#model", Input).value = model
        self.query_one("#models", OptionList).remove_class("visible")
        self.query_one("#setup-error", Static).update("")
        self.query_one("#del-provider", Button).display = p.is_user

    def _reload_options(self, select_id: str) -> None:
        select = self.query_one("#provider", Select)
        select.set_options(self._options())
        select.value = select_id
        self._fill(select_id)

    @on(Button.Pressed, "#add-provider")
    def _add_provider(self) -> None:
        def done(result: dict | None) -> None:
            if not result:
                return
            existing = {d["id"] for d in self.settings.custom_providers}
            pid = providers.make_id(result["name"], existing)
            self.settings.custom_providers.append({"id": pid, "name": result["name"], "base_url": result["base_url"]})
            if result["api_key"]:
                self.auth[pid] = Credentials(api_key=result["api_key"])
                save_auth(self.auth, self.base)
            save_settings(self.settings, self.base)
            self._reload_options(pid)
            self.notify(tr('Провайдер «{0}» добавлен. Выберите модель.', result['name']), timeout=3)

        self.app.push_screen(ProviderEditScreen(), done)

    @on(Button.Pressed, "#del-provider")
    def _delete_provider(self) -> None:
        pid = self.provider_id
        if not pid.startswith("user:"):
            return
        self.settings.custom_providers = [d for d in self.settings.custom_providers if d["id"] != pid]
        self.auth.pop(pid, None)
        if self.settings.provider == pid:
            self.settings.provider, self.settings.model = "", ""
        save_auth(self.auth, self.base)
        save_settings(self.settings, self.base)
        self._reload_options("deepseek")

    def _current_key(self) -> str:
        typed = self.query_one("#key", Input).value.strip()
        return typed or self.auth.get(self.provider_id, Credentials()).api_key

    @on(Button.Pressed, "#fetch")
    def _fetch_pressed(self) -> None:
        self.fetch_models()

    @work(exclusive=True)
    async def fetch_models(self) -> None:
        error = self.query_one("#setup-error", Static)
        error.update(tr("Загружаю список моделей…"))
        base_url = self.query_one("#base-url", Input).value.strip()
        client = LLMClient(base_url, self._current_key(), timeout=30)
        try:
            models = await client.list_models()
        except LLMError as e:
            error.update(str(e))
            return
        finally:
            await client.aclose()
        if not models:
            error.update(tr("Сервер не вернул список. Впишите название модели вручную."))
            return
        self._model_ids = models
        box = self.query_one("#models", OptionList)
        box.clear_options()
        box.add_options(models)
        box.add_class("visible")
        error.update(tr('Моделей: {0}. Выберите стрелками и Enter.', len(models)))
        box.focus()

    @on(OptionList.OptionSelected, "#models")
    def _model_picked(self, event: OptionList.OptionSelected) -> None:
        self.query_one("#model", Input).value = self._model_ids[event.option_index]
        self.query_one("#models", OptionList).remove_class("visible")

    @on(Button.Pressed, "#save")
    def _save(self) -> None:
        pid = self.provider_id
        p = providers.get(pid, self.settings)
        base_url = self.query_one("#base-url", Input).value.strip()
        model = self.query_one("#model", Input).value.strip()
        typed_key = self.query_one("#key", Input).value.strip()
        error = self.query_one("#setup-error", Static)
        if not base_url.startswith(("http://", "https://")):
            error.update(tr("Укажите адрес сервера, начиная с http:// или https://"))
            return
        if p.needs_key and not self._current_key():
            error.update(tr("Вставьте API-ключ."))
            return
        if not model:
            error.update(tr("Укажите модель или нажмите «Список моделей»."))
            return
        cred = self.auth.get(pid, Credentials())
        if typed_key:
            cred.api_key = typed_key
        if p.is_user:  # the address belongs to the provider entry itself
            for d in self.settings.custom_providers:
                if d["id"] == pid:
                    d["base_url"] = base_url
            cred.base_url = ""
        else:
            cred.base_url = "" if base_url == p.base_url else base_url
        self.auth[pid] = cred
        self.settings.provider, self.settings.model = pid, model
        save_auth(self.auth, self.base)
        save_settings(self.settings, self.base)
        self.dismiss(True)

    @on(Button.Pressed, "#cancel")
    def action_cancel(self) -> None:
        self.dismiss(False)


class ThemeScreen(ModalScreen[str | None]):
    """Theme picker with live preview."""

    BINDINGS = [("escape", "cancel", tr("Отмена"))]

    def __init__(self, themes: list[tuple[str, str]], current: str) -> None:
        super().__init__()
        self.themes = themes
        self.original = current

    def compose(self) -> ComposeResult:
        with Vertical(id="dialog"):
            yield Label(tr("Тема"), classes="dialog-title")
            yield OptionList(*[label for _name, label in self.themes], id="theme-list")
            yield Label(tr("↑↓ — посмотреть · Enter — выбрать · Esc — отмена"), classes="hint")

    def on_mount(self) -> None:
        box = self.query_one("#theme-list", OptionList)
        names = [n for n, _ in self.themes]
        if self.original in names:
            box.highlighted = names.index(self.original)
        box.focus()

    @on(OptionList.OptionHighlighted, "#theme-list")
    def _preview(self, event: OptionList.OptionHighlighted) -> None:
        try:
            self.app.theme = self.themes[event.option_index][0]
        except Exception:
            pass

    @on(OptionList.OptionSelected, "#theme-list")
    def _picked(self, event: OptionList.OptionSelected) -> None:
        self.dismiss(self.themes[event.option_index][0])

    def action_cancel(self) -> None:
        try:
            self.app.theme = self.original
        except Exception:
            pass
        self.dismiss(None)


class SessionsScreen(ModalScreen[str | None]):
    BINDINGS = [("escape", "close", tr("Закрыть"))]

    def __init__(self, sessions: list) -> None:
        super().__init__()
        self.sessions = sessions

    def compose(self) -> ComposeResult:
        with Vertical(id="dialog"):
            yield Label(tr("Сессии в этой папке"), classes="dialog-title")
            if not self.sessions:
                yield Label(tr("Пока пусто."), classes="hint")
            else:
                yield OptionList(*[
                    f"{datetime.fromtimestamp(s.updated):%d.%m %H:%M}  {s.title}" for s in self.sessions
                ], id="session-list")
            yield Label(tr("Enter — открыть, Esc — закрыть"), classes="hint")

    @on(OptionList.OptionSelected)
    def _picked(self, event: OptionList.OptionSelected) -> None:
        self.dismiss(self.sessions[event.option_index].id)

    def action_close(self) -> None:
        self.dismiss(None)


HELP = tr("""\
# ANIX Code

| Клавиши | Что делают |
|---|---|
| `Enter` | отправить |
| `Ctrl+J` | новая строка |
| `↑` / `↓` | история запросов |
| `Tab` | режим **Build** (правит код) / **Plan** (только читает и планирует) |
| `Esc` | остановить агента |
| `Ctrl+P` | выбрать проект (папку / репозиторий), клонировать с GitHub |
| `F2` | полный доступ вкл/выкл (агент не спрашивает разрешения) |
| `Ctrl+O` | модель и API-ключ |
| `Ctrl+N` | новая сессия |
| `Ctrl+S` | прошлые сессии |
| `Ctrl+T` | выбрать тему |
| `Ctrl+Q` | выход |

Быстрый ввод:
- `/` — список команд с подсказками (↑↓, Tab — дописать, Enter — выполнить)
- `!команда` — выполнить команду в терминале самому; агент увидит вывод
- `# текст` — записать правило в `AGENTS.md` («# используй pnpm, а не npm»)
- `Shift+Tab` или `Tab` — Build / Plan

Ещё:
- `/init` — агент изучит проект и напишет `AGENTS.md` с правилами для себя
- `@путь` в запросе — прикрепить файл или папку (после `@` появятся подсказки: ↑↓, Enter/Tab)
- `/undo` — откатить правки файлов из последнего ответа агента (можно несколько раз подряд)
- `/compact` — сжать историю вручную (обычно это происходит само при заполнении контекста)
- `/project` — выбрать проект; клик по пути в шапке делает то же самое
- `/connect`, `/new`, `/sessions`, `/theme`, `/help`, `/exit`

Внизу справа видно, на сколько заполнен контекст модели. При 80% история сжимается автоматически.
При сбоях сети и лимитах запросов агент сам повторяет запрос до 4 раз.
Когда агент хочет изменить файл или выполнить команду, над полем ввода появляется строка:
`Enter` — разрешить, `Esc` — отклонить, текст + `Enter` — отклонить и объяснить, как надо.
Опасные команды (удаление, `git push --force`, форматирование…) подсвечиваются красным.
**Полный доступ** (`F2` или клик внизу справа): агент работает без вопросов.

Клик по карточке действия показывает diff или вывод команды.
Правила проекта для агента можно написать в `AGENTS.md` в корне проекта.
""")


class HelpScreen(ModalScreen[None]):
    BINDINGS = [("escape", "close", tr("Закрыть")), ("f1", "close", tr("Закрыть")), ("q", "close", tr("Закрыть"))]

    def __init__(self, commands_list: list | None = None) -> None:
        super().__init__()
        self.commands_list = commands_list or []

    def compose(self) -> ComposeResult:
        seen, rows = set(), []
        for c in self.commands_list:
            if c.description in seen and c.action:
                continue  # aliases (/clear, /models…) once
            seen.add(c.description)
            src = tr(" *(свой)*") if c.source != "builtin" else ""
            rows.append(f"| `/{c.name}` {c.args_hint} | {c.description}{src} |")
        table = "\n".join([tr("| Команда | Что делает |"), "|---|---|", *rows])
        with VerticalScroll(id="dialog"):
            yield Markdown(HELP + tr("\n## Команды\n\n") + table + "\n\n" + CUSTOM_HELP)
            yield Label(tr("Esc — закрыть"), classes="hint")

    def action_close(self) -> None:
        self.dismiss(None)


CUSTOM_HELP = tr("""\
## Свои команды

Положите Markdown-файл в `.anixcode/commands/имя.md` (для проекта) или в папку настроек
`commands/имя.md` (для всех проектов). Текст файла станет запросом агенту, `$ARGUMENTS` заменится
на всё, что написано после команды, `$1`, `$2` — на отдельные слова. Описание для подсказок:

```markdown
---
description: прогнать линтер и всё починить
---
Run the linter for $ARGUMENTS and fix every warning.
```
""")


class FolderTree(DirectoryTree):
    """Folders only, no hidden or junk folders."""

    def filter_paths(self, paths):
        out = []
        for p in paths:
            try:
                if p.is_dir() and not p.name.startswith(".") and p.name not in IGNORE_DIRS:
                    out.append(p)
            except OSError:
                continue
        return out


class ProjectScreen(ModalScreen[str | None]):
    """Pick the folder (repository) the agent works in: recent, found repos, browse, or git clone."""

    BINDINGS = [("escape", "cancel", tr("Отмена"))]

    def __init__(self, current: Path, recent: list[str]) -> None:
        super().__init__()
        self.current = current
        self.recent = [r for r in recent if Path(r).is_dir()]
        self._repos: list[Path] = []
        self.drive_list = projects.drives()

    def compose(self) -> ComposeResult:
        with Vertical(id="dialog", classes="wide"):
            yield Label(tr("Проект: в какой папке работать"), classes="dialog-title")
            with Horizontal(id="proj-row"):
                yield Input(value=str(self.current), placeholder=tr("Путь к папке или ссылка на git-репозиторий"),
                            id="proj-path")
                yield Button(tr("Открыть"), id="proj-open", variant="primary")
            yield Static(id="proj-status", classes="hint")
            with TabbedContent(id="proj-tabs"):
                with TabPane(tr("Недавние"), id="tab-recent"):
                    if self.recent:
                        yield OptionList(*[self._label(Path(r)) for r in self.recent], id="recent-list")
                    else:
                        yield Label(tr("Пока пусто: здесь появятся папки, которые вы открывали."), classes="hint")
                with TabPane(tr("Репозитории"), id="tab-repos"):
                    yield Label(tr("Ищу git-репозитории…"), id="repos-hint", classes="hint")
                    yield OptionList(id="repos-list")
                with TabPane(tr("Обзор"), id="tab-browse"):
                    with Horizontal(id="drive-row"):
                        yield Button(tr("🏠 Домой"), id="drive-home", classes="drive")
                        for i, d in enumerate(self.drive_list):
                            yield Button(str(d).rstrip("\\/") or "/", id=f"drive-{i}", classes="drive")
                    yield FolderTree(str(Path.home()), id="folder-tree")
            yield Label(tr("Кликните папку в дереве (она раскроется и подставится в строку) и нажмите «Открыть». "
                        "Ссылка https://github.com/… — склонировать. Esc — отмена."), classes="hint")

    @staticmethod
    def _label(p: Path) -> Text:
        mark = "● " if projects.is_git_repo(p) else "  "
        text = Text(mark, style="green")
        text.append(p.name or str(p), style="bold")
        text.append(f"   {p}", style="dim")
        return text

    def on_mount(self) -> None:
        self.query_one("#proj-path", Input).focus()
        self.query_one(FolderTree).root.expand()
        self.scan_repos()

    @on(Button.Pressed, ".drive")
    def _drive_pressed(self, event: Button.Pressed) -> None:
        bid = event.button.id or ""
        target = Path.home() if bid == "drive-home" else self.drive_list[int(bid.split("-")[1])]
        tree = self.query_one(FolderTree)
        tree.path = target
        self._set_path(target)
        self.call_after_refresh(tree.root.expand)

    @work(exclusive=True, group="scan")
    async def scan_repos(self) -> None:
        repos = await asyncio.to_thread(projects.find_repos, projects.search_roots())
        self._repos = repos
        hint = self.query_one("#repos-hint", Label)
        box = self.query_one("#repos-list", OptionList)
        if not repos:
            hint.update(tr("Не нашёл репозиториев в Desktop, Documents, Projects и похожих папках. Используйте «Обзор»."))
            return
        hint.update(tr('Найдено: {0}', len(repos)))
        box.add_options([self._label(p) for p in repos])

    def _set_path(self, p: Path) -> None:
        self.query_one("#proj-path", Input).value = str(p)

    @on(OptionList.OptionSelected, "#recent-list")
    def _recent_picked(self, event: OptionList.OptionSelected) -> None:
        self._open(Path(self.recent[event.option_index]))

    @on(OptionList.OptionSelected, "#repos-list")
    def _repo_picked(self, event: OptionList.OptionSelected) -> None:
        self._open(self._repos[event.option_index])

    @on(DirectoryTree.DirectorySelected, "#folder-tree")
    def _folder_picked(self, event: DirectoryTree.DirectorySelected) -> None:
        self._set_path(Path(event.path))

    @on(Input.Submitted, "#proj-path")
    @on(Button.Pressed, "#proj-open")
    def _open_pressed(self) -> None:
        value = self.query_one("#proj-path", Input).value.strip().strip('"')
        if projects.GIT_URL.match(value) and not Path(value).exists():
            self.clone_repo(value)
            return
        self._open(Path(value).expanduser())

    def _open(self, p: Path) -> None:
        status = self.query_one("#proj-status", Static)
        if not p.is_dir():
            status.update(Text(tr('Нет такой папки: {0}', p), style="red"))
            return
        self.dismiss(str(p.resolve()))

    @work(exclusive=True, group="clone")
    async def clone_repo(self, url: str) -> None:
        status = self.query_one("#proj-status", Static)
        parent = self.current if not projects.is_unsuitable(self.current) and not projects.is_git_repo(self.current) \
            else Path.home() / "Projects"
        status.update(tr('Клонирую в {0}…', parent))
        try:
            target = await asyncio.to_thread(projects.clone, url, parent)
        except RuntimeError as e:
            status.update(Text(str(e), style="red"))
            return
        self.dismiss(str(target))

    def action_cancel(self) -> None:
        self.dismiss(None)
