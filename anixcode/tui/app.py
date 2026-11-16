"""The terminal app."""
from __future__ import annotations
from ..i18n import tr

import asyncio
import time
from pathlib import Path
from typing import Any

from rich.text import Text
from textual import on, work
from textual.app import App, ComposeResult
from textual.binding import Binding
from textual.containers import Horizontal, Vertical, VerticalScroll
from textual.screen import ModalScreen
from textual.widgets import OptionList, Static, TextArea

from .. import __version__, commands, projects, providers
from ..agent import (
    Agent, AgentError, Attached, Compacted, Compacting, PermissionRequest, StepFinished, ToolFinished, ToolStarted,
)
from ..danger import check_command
from ..mcp import McpError, McpManager, parse_add, status_rows
from ..mentions import index_files, rank_paths
from ..tools import ToolContext, default_tools
from ..airpc import AirpcReporter
from ..llm import LLMClient, LLMError, ReasoningDelta, Retrying, TextDelta
from ..settings import Credentials, Settings, config_dir, load_auth, load_settings, save_settings
from ..storage import Session, Store
from ..tools import ToolResult
from .anim import THEME_LABELS, ActivityIndicator, AnimatedLogo, Gradient, make_themes, palette
from .screens import HelpScreen, ProjectScreen, SessionsScreen, SetupScreen, ThemeScreen
from .widgets import (
    AssistantMessage, Chip, ErrorMessage, MentionList, Notice, PermissionBar, PromptInput, ReasoningBlock, ToolCard,
    UserMessage,
)

def _tokens(n: int) -> str:
    return f"{n / 1000:.1f}k" if n >= 1000 else str(n)


class Brand(Static):
    """Header: logo + project path. Click to switch project."""

    def on_click(self) -> None:
        self.app.action_projects()


class AnixApp(App):
    CSS_PATH = "app.tcss"
    TITLE = "ANIX Code"
    ENABLE_COMMAND_PALETTE = False  # Ctrl+P is our project picker
    BINDINGS = [
        Binding("tab", "toggle_mode", "Build/Plan", priority=True, show=False),
        Binding("shift+tab", "toggle_mode", "Build/Plan", priority=True, show=False),
        Binding("escape", "interrupt", tr("Стоп"), show=False),
        Binding("ctrl+o", "connect", tr("Модель"), show=False),
        Binding("f2", "toggle_access", tr("Полный доступ"), priority=True, show=False),
        Binding("ctrl+p", "projects", tr("Проект"), priority=True, show=False),
        Binding("ctrl+n", "new_session", tr("Новая"), show=False),
        Binding("ctrl+s", "sessions", tr("Сессии"), show=False),
        Binding("ctrl+t", "cycle_theme", tr("Тема"), show=False),
        Binding("f1", "help", tr("Помощь"), show=False),
        Binding("ctrl+q", "quit", tr("Выход"), priority=True, show=False),
    ]

    def __init__(self, root: Path, settings: Settings, store: Store, base: Path | None = None,
                 pick_project: bool = False) -> None:
        super().__init__()
        self._pick_project = pick_project
        self._file_index: list[str] | None = None
        self._pending: tuple[PermissionRequest, asyncio.Future] | None = None
        self._cards: dict[str, ToolCard] = {}
        self._index_time = 0.0
        self._indexing = False
        self.project_root = root.resolve()
        self.settings = settings
        self.store = store
        self.config_base = base
        self.auth = load_auth(base)
        self.session: Session = store.create(str(self.project_root), settings.model)
        self.agent: Agent | None = None
        self.mcp: McpManager | None = None
        self._llm_client: LLMClient | None = None
        self._agent_busy = False
        self._agent_mode = "build"

    # -- layout --------------------------------------------------------------------
    def compose(self) -> ComposeResult:
        with Horizontal(id="header"):
            yield Brand(id="brand")
            yield Static(id="mode")
        with VerticalScroll(id="chat"):
            yield self._welcome()
        with Vertical(id="composer"):
            yield ActivityIndicator(id="activity")
            yield PermissionBar(self._resolve_permission, id="permission")
            yield MentionList(id="mentions")
            yield PromptInput(id="prompt")
        with Horizontal(id="statusbar"):
            yield Static(id="status-model")
            yield Static(id="status-usage")
            yield Chip("", self.action_toggle_access, id="status-access")

    def _welcome(self) -> Static:
        footer = Text()
        footer.append(f"v{__version__}\n\n", style="dim")
        footer.append(tr("Опишите задачу: агент сам прочитает код, внесёт правки и запустит команды.\n"), style="dim")
        footer.append(tr("Tab: Build/Plan · Ctrl+O: модель · F1: все клавиши"), style="dim")
        return AnimatedLogo(footer, animate=self.settings.animations, id="welcome")

    def on_mount(self) -> None:
        for theme in make_themes():
            try:
                self.register_theme(theme)
            except Exception:
                pass
        self._apply_theme(self.settings.theme)
        self.set_interval(1 / 10, self._pulse_brand)
        self._build_agent()
        self.run_worker(self._start_mcp(), group="mcp", exclusive=True)
        self._refresh_bars()
        prompt = self.query_one(PromptInput)
        prompt.mention_list = self.query_one(MentionList)
        prompt.focus()
        self.call_after_refresh(self._warn_if_unsuitable)
        if not self._pick_project:
            self._remember_project()
        if not self.agent:
            self.call_after_refresh(self.action_connect)
        elif self._pick_project:
            self.call_after_refresh(self.action_projects)

    def _apply_theme(self, name: str) -> None:
        try:
            if name in getattr(self, "available_themes", {}):
                self.theme = name
        except Exception:
            pass

    # -- agent wiring ----------------------------------------------------------------
    def _build_agent(self) -> None:
        old_mode = self.agent.mode if self.agent else self._agent_mode
        if not self.settings.ready:
            self.agent = None
            return
        p = providers.get(self.settings.provider, self.settings)
        cred = self.auth.get(self.settings.provider, Credentials())
        if self._llm_client is not None:
            asyncio.create_task(self._llm_client.aclose())
        self._llm_client = LLMClient(cred.base_url or p.base_url, cred.api_key)
        self.agent = Agent(self._llm_client, self.settings.model, self.project_root, self.settings, ask=self._ask_permission,
                           reporter=AirpcReporter(self.settings.airpc), provider=self.settings.provider,
                           tools=default_tools() | (self.mcp.tools() if self.mcp else {}))
        self.agent.mode = old_mode

    async def _start_mcp(self) -> None:
        """Start the MCP servers from settings.json in the background; the chat works meanwhile."""
        if self.agent:
            self.agent.tools = default_tools()  # no stale tools of the old project while we restart
        if self.mcp is not None:
            await self.mcp.close()
            self.mcp = None
        if not self.settings.mcp_servers:
            return
        mcp = McpManager(self.settings.mcp_servers, self.project_root)
        await mcp.start()
        self.mcp = mcp
        if self.agent:
            self.agent.tools = default_tools() | mcp.tools()
        # toasts, not chat notices: the agent may already be streaming an answer into the chat
        if mcp.servers:
            self._notify_plain(tr("🔌 MCP подключены: {0}", ", ".join(f"{s.name} ({len(s.tools)})" for s in mcp.servers)),
                               timeout=3)
        for name, err in mcp.errors.items():
            self._notify_plain(tr("⚠ MCP {0}: {1}", name, err), severity="warning", timeout=8)

    async def _ask_permission(self, req: PermissionRequest) -> str:
        """Inline approval: a line above the prompt, Enter = yes, Esc = no."""
        fut: asyncio.Future[str] = asyncio.get_running_loop().create_future()
        self._pending = (req, fut)
        card = self._cards.get(req.call_id)
        if card is not None:
            card.show_pending(req.preview, req.kind)
        bar = self.query_one(PermissionBar)
        bar.show(req)
        prompt = self.query_one(PromptInput)
        prompt.permission_handler = self._resolve_permission
        prompt.focus()
        indicator = self.query_one(ActivityIndicator)
        indicator.set_phase(tr("Ждёт разрешения: Enter / Esc"), paused=True)
        self._scroll()
        try:
            return await fut
        finally:
            self._pending = None
            prompt.permission_handler = None
            bar.hide()
            indicator.set_phase(tr('Выполняет: {0}', req.title))

    def _resolve_permission(self, decision: str, feedback: str = "") -> None:
        if self._pending is None:
            return
        req, fut = self._pending
        if decision == "full":
            self._set_full_access(True)
            decision = "once"
        req.feedback = feedback
        if not fut.done():
            fut.set_result(decision)

    def _set_full_access(self, value: bool) -> None:
        self.settings.full_access = value
        save_settings(self.settings, self.config_base)
        self._refresh_bars()
        self.notify(tr("🔓 Полный доступ: агент правит файлы и запускает команды без вопросов") if value
                    else tr("🔒 Агент снова спрашивает разрешение"), timeout=3)

    def action_toggle_access(self) -> None:
        self._set_full_access(not self.settings.full_access)
        if self.settings.full_access and self._pending is not None:
            self._resolve_permission("once")

    def _refresh_bars(self) -> None:
        self._pulse_brand(force=True)
        mode = self.agent.mode if self.agent else self._agent_mode
        badge = self.query_one("#mode", Static)
        badge.update("PLAN" if mode == "plan" else "BUILD")
        badge.set_class(mode == "plan", "plan")
        if self.settings.ready:
            model = Text(self.settings.model, style="bold")
        else:
            model = Text(tr("модель не подключена · ctrl+o"), style="dim")
        s = self.session
        usage = Text(f"↑{_tokens(s.tokens_in)} ↓{_tokens(s.tokens_out)}", style="dim")
        if self.agent and s.messages:
            pct = int(self.agent.context_usage(s) * 100)
            usage.append(tr("   контекст "), style="dim")
            usage.append(f"{pct}%", style="bold red" if pct >= 80 else "bold yellow" if pct >= 60 else "dim")
        self.query_one("#status-model", Static).update(model)
        self.query_one("#status-usage", Static).update(usage)
        access = self.query_one("#status-access", Chip)
        if self.settings.full_access:
            access.update(Text(tr("🔓 полный доступ"), style="bold yellow"))
        else:
            access.update(Text(tr("🔒 спрашивать"), style="dim"))

    def _pulse_brand(self, force: bool = False) -> None:
        """Header diamond: theme color at rest, flowing gradient while the agent works."""
        if not (force or self._agent_busy):
            return
        if self._agent_busy and self.settings.animations:
            color = Gradient(palette(self)).at(asyncio.get_running_loop().time() * 0.4).hex
        else:
            color = palette(self)[0]
        brand = Text.assemble(("◆ ", f"bold {color}"), ("ANIX Code", "bold"), ("  " + self._short_root(), "dim"))
        self.query_one("#brand", Static).update(brand)

    def _short_root(self) -> str:
        home = Path.home()
        try:
            rel = self.project_root.relative_to(home).as_posix()
        except ValueError:
            return str(self.project_root)
        return "~" if rel in ("", ".") else f"~/{rel}"

    def _scroll(self) -> None:
        self.query_one("#chat", VerticalScroll).scroll_end(animate=False)

    # -- input -----------------------------------------------------------------------
    # -- @file suggestions ------------------------------------------------------------
    @on(TextArea.Changed, "#prompt")
    @on(TextArea.SelectionChanged, "#prompt")
    def _prompt_changed(self) -> None:
        self._update_mentions()

    def _commands(self) -> list[commands.Command]:
        return commands.all_commands(self.project_root, self.config_base or config_dir())

    def _update_mentions(self) -> None:
        prompt, ml = self.query_one(PromptInput), self.query_one(MentionList)
        query = prompt.suggestion_query()
        if query is None:
            ml.hide()
            return
        kind, prefix = query
        if kind == "command":
            found = commands.suggest(prefix, self._commands())
            if not found:
                ml.hide()
                return
            labels = []
            for c in found:
                label = Text.assemble((f"/{c.name}", "bold"), "  ", (c.description, "dim"))
                if c.args_hint:
                    label.append(f"  {c.args_hint}", style="dim italic")
                if c.source != "builtin":
                    label.append(tr("  · свой"), style="dim")
                labels.append(label)
            ml.show([c.name for c in found], labels, {c.name: c.takes_args for c in found})
            return
        if self._file_index is None or time.monotonic() - self._index_time > 30:
            self._refresh_index()
            if self._file_index is None:
                return
        items = rank_paths(self._file_index, prefix)
        if items:
            ml.show(items)
        else:
            ml.hide()

    @work(exclusive=True, group="index")
    async def _refresh_index(self) -> None:
        if self._indexing:
            return
        self._indexing = True
        try:
            root = self.project_root
            index = await asyncio.to_thread(index_files, root)
            if root == self.project_root:
                self._file_index, self._index_time = index, time.monotonic()
        finally:
            self._indexing = False
        self._update_mentions()

    @on(OptionList.OptionSelected, "#mentions")
    def _mention_clicked(self, event: OptionList.OptionSelected) -> None:
        ml = self.query_one(MentionList)
        prompt = self.query_one(PromptInput)
        prompt.accept_suggestion(ml.mention_items[event.option_index])
        prompt.focus()

    @on(PromptInput.Submitted)
    def _submitted(self, event: PromptInput.Submitted) -> None:
        self.query_one(MentionList).hide()
        text = event.text
        if text.startswith("/") and len(text) > 1 and not text.startswith("//"):
            self._command(text)
            return
        if self._agent_busy:
            self.notify(tr("Агент ещё работает. Esc — остановить."), severity="warning")
            return
        if text.startswith("!") and len(text) > 1:
            self.run_shell(text[1:].strip())  # like Claude Code: run it yourself, the agent sees the output
            return
        if text.startswith("#") and not text.startswith("##") and len(text) > 1:
            self._remember_note(text[1:].strip())
            return
        if not self.agent:
            self.action_connect()
            return
        self.run_agent(text)

    def _command(self, text: str) -> None:
        name, args = commands.parse(text)
        cmd = commands.find(name, self._commands())
        if cmd is None:
            self.notify(tr('Нет команды /{0}. Наберите / и посмотрите список.', name), severity="warning")
            return
        if cmd.template is not None:
            if not self.agent:
                self.action_connect()
                return
            if self._agent_busy:
                self.notify(tr("Агент ещё работает. Esc — остановить."), severity="warning")
                return
            if cmd.takes_args and not args and "$ARGUMENTS" in cmd.template and cmd.args_hint.startswith("<"):
                self.notify(f"/{name} {cmd.args_hint}", severity="warning")
                return
            if self.agent.mode == "plan" and name in ("init", "commit", "test", "fix"):
                self.action_toggle_mode()  # these need Build mode
            display = f"/{name}" + (f" {args}" if args else "") + f" · {cmd.description}"
            self.run_agent(commands.render(cmd.template, args), display=display)
            return
        if cmd.action == "mcp":  # the only action that takes arguments
            self.action_mcp(args)
            return
        actions = {
            "help": self.action_help, "new": self.action_new_session, "sessions": self.action_sessions,
            "undo": self.action_undo, "redo": self.action_redo, "compact": self.action_compact,
            "connect": self.action_connect, "project": self.action_projects, "theme": self.action_cycle_theme,
            "mode": self.action_toggle_mode, "permissions": self.action_toggle_access,
            "status": self.action_status, "export": self.action_export, "doctor": self.action_doctor,
            "language": self.action_language,
            "exit": self.exit,
        }
        actions[cmd.action]()

    def _remember_note(self, note: str) -> None:
        async def go() -> None:
            path = commands.append_memory(self.project_root, note)
            await self.query_one("#chat", VerticalScroll).mount(
                Notice(tr('📝 Записал в {0}: {1}  (агент учтёт это в следующих ответах)', path.name, note)))
            self._scroll()

        self.run_worker(go())

    @work(exclusive=True, group="agent")
    async def run_shell(self, command: str) -> None:
        chat = self.query_one("#chat", VerticalScroll)
        for w in chat.query("#welcome"):
            await w.remove()
        await chat.mount(UserMessage(f"! {command}"))
        risk = check_command(command, self.project_root)
        card = ToolCard("bash", command, "exec", full_title=command, dangerous=risk.dangerous)
        await chat.mount(card)
        self._agent_busy = True
        self._refresh_bars()
        self._scroll()
        try:
            ctx = self.agent.ctx if self.agent else ToolContext(self.project_root)
            result = await default_tools()["bash"].run({"command": command}, ctx)
            card.finish(result)
            card.add_class("open")
            self.session.add({"role": "user", "content": (
                f"[I ran a command myself in the project folder]\n$ {command}\n\n{result.output[:8000]}")})
        finally:
            self._agent_busy = False
            self._refresh_bars()
            self._scroll()

    # -- the agent run -------------------------------------------------------------
    @work(exclusive=True, group="agent")
    async def run_agent(self, text: str, display: str | None = None) -> None:
        assert self.agent is not None
        chat = self.query_one("#chat", VerticalScroll)
        for w in chat.query("#welcome"):
            await w.remove()
        await chat.mount(UserMessage(display or text))
        self._agent_busy = True
        self._refresh_bars()
        self._scroll()
        assistant: AssistantMessage | None = None
        reasoning: ReasoningBlock | None = None
        cards = self._cards
        cards.clear()
        indicator = self.query_one(ActivityIndicator)
        indicator.motion = self.settings.animations
        indicator.start()
        try:
            async for ev in self.agent.run(self.session, text):
                if isinstance(ev, Attached):
                    await chat.mount(Notice(tr("📎 Прикреплено: ") + ", ".join(ev.paths)))
                if isinstance(ev, (ReasoningDelta, TextDelta)):
                    indicator.chars += len(ev.text)
                if isinstance(ev, ReasoningDelta):
                    if reasoning is None:
                        indicator.set_phase(tr("Размышляет"))
                        reasoning = ReasoningBlock()
                        await chat.mount(reasoning)
                    reasoning.append(ev.text)
                elif isinstance(ev, TextDelta):
                    if reasoning is not None:
                        reasoning.finish()
                        reasoning = None
                    if assistant is None:
                        indicator.set_phase(tr("Пишет ответ"))
                        assistant = AssistantMessage()
                        await chat.mount(assistant)
                    assistant.append_text(ev.text)
                elif isinstance(ev, ToolStarted):
                    if assistant is not None:
                        await assistant.finish()
                        assistant = None
                    if reasoning is not None:
                        reasoning.finish()
                        reasoning = None
                    indicator.set_phase(tr('Выполняет: {0}', ev.title))
                    full = str(self.agent.last_args.get("command", "")) if ev.tool == "bash" else ""
                    card = ToolCard(ev.tool, ev.title, ev.kind, full_title=full, dangerous=ev.dangerous)
                    cards[ev.call_id] = card
                    await chat.mount(card)
                elif isinstance(ev, ToolFinished):
                    if ev.call_id in cards:
                        cards[ev.call_id].finish(ev.result)
                    indicator.set_phase(tr("Думает"))
                elif isinstance(ev, StepFinished):
                    self._refresh_bars()
                elif isinstance(ev, AgentError):
                    await chat.mount(ErrorMessage(ev.message))
                elif isinstance(ev, Retrying):
                    indicator.set_phase(tr('Повтор {0}/{1} через {2:.0f}с: {3}', ev.attempt, ev.max_attempts, ev.delay, ev.reason))
                elif isinstance(ev, Compacting):
                    if assistant is not None:
                        await assistant.finish()
                        assistant = None
                    indicator.set_phase(tr("Сжимает историю"))
                elif isinstance(ev, Compacted):
                    await chat.mount(Notice(self._compacted_text(ev)))
                    indicator.set_phase(tr("Думает"))
                    self._refresh_bars()
                self._scroll()
        except asyncio.CancelledError:
            await chat.mount(Notice(tr("⏹ Остановлено")))
        finally:
            indicator.stop()
            if assistant is not None:
                await assistant.finish()
            if reasoning is not None:
                reasoning.finish()
            self._agent_busy = False
            self._refresh_bars()
            self._scroll()

    # -- actions ---------------------------------------------------------------------
    def _warn_if_unsuitable(self) -> None:
        if not projects.is_unsuitable(self.project_root):
            return

        async def go() -> None:
            await self.query_one("#chat", VerticalScroll).mount(Notice(Text(
                tr('⚠ Агент сейчас работает в {0}: это не папка проекта, а весь профиль или диск. Поиск будет медленным, а правки могут задеть что угодно. Выберите проект: Ctrl+P.', self.project_root),
                style="yellow")))

        self.run_worker(go())

    def _remember_project(self) -> None:
        self.settings.recent_projects = projects.remember(self.settings.recent_projects, self.project_root)
        save_settings(self.settings, self.config_base)

    def action_projects(self) -> None:
        if isinstance(self.screen, ProjectScreen):
            return
        if self._agent_busy:
            self.notify(tr("Сначала остановите агента (Esc)."), severity="warning")
            return

        def done(path: str | None) -> None:
            if path:
                self.run_worker(self._switch_project(Path(path)))
            else:
                self.query_one(PromptInput).focus()

        self.push_screen(ProjectScreen(self.project_root, self.settings.recent_projects), done)

    async def _switch_project(self, path: Path) -> None:
        await self._reset_chat()
        self.project_root = path.resolve()
        self._pick_project = False
        self._file_index = None
        self._remember_project()
        self.session = self.store.create(str(self.project_root), self.settings.model)
        self._build_agent()
        self.run_worker(self._start_mcp(), group="mcp", exclusive=True)
        await self.query_one("#chat", VerticalScroll).mount(self._welcome())
        self._refresh_bars()
        self._warn_if_unsuitable()
        git = " (git)" if projects.is_git_repo(self.project_root) else ""
        self.notify(tr('Проект: {0}{1}', self.project_root, git), timeout=3)
        self.query_one(PromptInput).focus()

    @staticmethod
    def _compacted_text(ev: Compacted) -> str:
        how = tr("старые шаги свёрнуты в сводку") if ev.summarized else tr("убраны старые выводы команд")
        return tr('🗜 История сжата: ~{0} → ~{1} токенов ({2})', _tokens(ev.before), _tokens(ev.after), how)

    def action_undo(self) -> None:
        if self._agent_busy or not self.agent:
            self.notify(tr("Сначала остановите агента (Esc)."), severity="warning")
            return

        async def go() -> None:
            result = self.agent.undo(self.session)
            chat = self.query_one("#chat", VerticalScroll)
            if result is None:
                await chat.mount(Notice(tr("↶ Откатывать нечего: агент не правил файлы в этой сессии (команды в терминале не откатываются).")))
            else:
                parts = [tr('восстановлено: {0}', ', '.join(result.restored)) if result.restored else "",
                         tr('удалены созданные: {0}', ', '.join(result.deleted)) if result.deleted else ""]
                await chat.mount(Notice(tr("↶ Правки последнего ответа откачены, ") + "; ".join(p for p in parts if p)
                                        + tr(". Команды в терминале не откатываются.")))
            self._scroll()

        self.run_worker(go())

    def action_redo(self) -> None:
        if self._agent_busy or not self.agent:
            self.notify(tr("Сначала остановите агента (Esc)."), severity="warning")
            return

        async def go() -> None:
            result = self.agent.redo(self.session)
            chat = self.query_one("#chat", VerticalScroll)
            if result is None:
                await chat.mount(Notice(tr("↷ Возвращать нечего: сначала /undo.")))
            else:
                await chat.mount(Notice(tr("↷ Откаченные правки возвращены: ") + ", ".join(result.restored + result.deleted)))
            self._scroll()

        self.run_worker(go())

    def action_language(self) -> None:
        from ..i18n import LANG, LANGUAGES

        new = "en" if (self.settings.language or LANG) == "ru" else "ru"
        self.settings.language = new
        save_settings(self.settings, self.config_base)
        self.notify(tr("Язык: {0}. Перезапустите ANIX Code, чтобы применить.", LANGUAGES[new]), timeout=6)

    def action_status(self) -> None:
        s = self.session
        git = "git" if projects.is_git_repo(self.project_root) else tr("не git")
        lines = Text()
        lines.append(tr("ℹ Сводка\n"), style="bold")
        rows = [
            (tr("Проект"), f"{self.project_root}  ({git})"),
            (tr("Модель"), f"{providers.get(self.settings.provider, self.settings).name} · {self.settings.model}"
             if self.settings.ready else tr("не подключена (/connect)")),
            (tr("Режим"), ("PLAN" if self.agent and self.agent.mode == "plan" else "BUILD") + " · "
             + (tr("полный доступ") if self.settings.full_access else tr("спрашивать разрешение"))),
            (tr("Токены"), f"↑{_tokens(s.tokens_in)} ↓{_tokens(s.tokens_out)}"),
        ]
        if self.agent:
            rows.append((tr("Контекст"), tr('{0}% из ~{1}', int(self.agent.context_usage(s) * 100), _tokens(self.agent.window))))
        custom = [c for c in self._commands() if c.source != "builtin"]
        rows += [(tr("Сессия"), tr('{0} · сообщений: {1}', s.title, len(s.messages))),
                 ("AGENTS.md", tr("есть") if (self.project_root / "AGENTS.md").exists() else tr("нет (/init создаст)")),
                 (tr("Свои команды"), ", ".join("/" + c.name for c in custom) or tr("нет"))]
        if self.mcp and (self.mcp.servers or self.mcp.errors):
            rows.append(("MCP", ", ".join(f"{n} {'✓' if ok else '✗'} {d}" for n, ok, d in self.mcp.summary())))
        for k, v in rows:
            lines.append(f"  {k:<13}", style="dim")
            lines.append(v + "\n")

        async def go() -> None:
            await self.query_one("#chat", VerticalScroll).mount(Notice(lines))
            self._scroll()

        self.run_worker(go())

    def action_export(self) -> None:
        from datetime import datetime

        s = self.session
        if not s.messages:
            self.notify(tr("Сессия пока пустая."), severity="warning")
            return
        folder = (self.config_base or config_dir()) / "exports"
        folder.mkdir(parents=True, exist_ok=True)
        slug = "".join(ch if ch.isalnum() else "-" for ch in s.title.lower())[:40].strip("-") or "session"
        path = folder / f"{datetime.now():%Y-%m-%d_%H-%M}_{slug}.md"
        path.write_text(commands.session_markdown(s.title, self.settings.model, s.messages), encoding="utf-8")

        async def go() -> None:
            await self.query_one("#chat", VerticalScroll).mount(Notice(tr('💾 Сессия сохранена: {0}', path)))
            self._scroll()

        self.run_worker(go())

    def action_doctor(self) -> None:
        async def go() -> None:
            import platform
            import shutil
            import subprocess
            from importlib.metadata import PackageNotFoundError, version

            from ..airpc import AirpcReporter

            out = Text(tr("🩺 Проверка\n"), style="bold")

            def row(ok: bool | None, name: str, detail: str) -> None:
                mark, style = ("✓", "green") if ok else ("✗", "red") if ok is False else ("–", "yellow")
                out.append(f"  {mark} ", style=style)
                out.append(f"{name:<12}", style="bold")
                out.append(detail + "\n", style="dim")

            row(True, "Python", platform.python_version())
            for pkg in ("textual", "httpx"):
                try:
                    row(True, pkg, version(pkg))
                except PackageNotFoundError:
                    row(False, pkg, tr("не установлен: pip install -e ."))
            git = shutil.which("git")
            if git:
                ver = await asyncio.to_thread(subprocess.run, [git, "--version"], capture_output=True, text=True)
                row(True, "git", ver.stdout.strip())
            else:
                row(False, "git", tr("не найден: https://git-scm.com (нужен для /commit, /review, клонирования)"))
            row(True if shutil.which("rg") else None, "ripgrep",
                tr("есть, поиск быстрый") if shutil.which("rg") else tr("нет (необязательно, ускоряет поиск)"))
            row(projects.is_git_repo(self.project_root), tr("проект"),
                f"{self.project_root}" + ("" if projects.is_git_repo(self.project_root) else tr(" — не git-репозиторий")))
            row((self.project_root / "AGENTS.md").exists() or None, "AGENTS.md",
                tr("есть") if (self.project_root / "AGENTS.md").exists() else tr("нет, создайте: /init"))
            if self._llm_client is not None:
                try:
                    models = await asyncio.wait_for(self._llm_client.list_models(), timeout=20)
                    row(True, tr("модель"), tr('{0} · сервер отвечает, моделей: {1}', self.settings.model, len(models)))
                except (LLMError, asyncio.TimeoutError) as e:
                    row(False, tr("модель"), str(e).splitlines()[0] if str(e) else tr("нет ответа"))
            else:
                row(False, tr("модель"), tr("не подключена: /connect"))
            row(True if AirpcReporter(self.settings.airpc).active else None, "AI-RPC",
                tr("найден, статус уйдёт в Discord") if AirpcReporter(self.settings.airpc).active else tr("не запущен (необязательно)"))
            for name, ok, detail in (self.mcp.summary() if self.mcp else []):
                row(ok, f"MCP {name}", detail)
            await self.query_one("#chat", VerticalScroll).mount(Notice(out))
            self._scroll()

        self.run_worker(go())

    # -- /mcp ------------------------------------------------------------------------
    def action_mcp(self, args: str = "") -> None:
        sub, _, rest = args.strip().partition(" ")
        sub, rest = sub.lower(), rest.strip()
        if sub in ("", "list", "ls"):
            self._post_notice(self._mcp_list())
            return
        if sub == "tools":
            self._post_notice(self._mcp_tools(rest))
            return
        if self._agent_busy:  # don't swap tools under a running agent
            self._notify_plain(tr("Сначала остановите агента (Esc)."), severity="warning")
            return
        servers = self.settings.mcp_servers
        try:
            if sub == "add":
                name, cfg = parse_add(rest)
                note = (tr("MCP {0} обновлён, подключаю…", name) if name in servers
                        else tr("MCP {0} добавлен, подключаю…", name))
                servers[name] = cfg
            elif sub in ("remove", "rm"):
                name = self._mcp_name(rest)
                del servers[name]
                note = tr("MCP {0} удалён", name)
            elif sub in ("on", "off"):
                name = self._mcp_name(rest)
                cfg = {k: v for k, v in servers[name].items() if k != "disabled"}
                if sub == "off":
                    cfg["disabled"] = True
                servers[name] = cfg
                note = tr("MCP {0} выключен", name) if sub == "off" else tr("MCP {0} включён, подключаю…", name)
            elif sub in ("reload", "restart"):
                self.settings.mcp_servers = load_settings(self.config_base).mcp_servers
                note = tr("Перезапускаю MCP-серверы…")
            else:
                raise McpError(tr("Нет подкоманды «{0}». Есть: add, remove, on, off, tools, reload", sub))
        except McpError as e:
            self._notify_plain(str(e), severity="warning", timeout=8)
            return
        if sub not in ("reload", "restart"):
            save_settings(self.settings, self.config_base)
        self._notify_plain("🔌 " + note, timeout=3)
        self.run_worker(self._start_mcp(), group="mcp", exclusive=True)

    def _mcp_name(self, rest: str) -> str:
        name = rest.split()[0] if rest else ""
        if name not in self.settings.mcp_servers:
            known = ", ".join(self.settings.mcp_servers) or tr("нет")
            raise McpError(tr("Нет MCP-сервера «{0}». Есть: {1}", name, known))
        return name

    def _mcp_list(self) -> Text:
        out = Text(tr("🔌 MCP-серверы\n"), style="bold")
        rows = status_rows(self.settings.mcp_servers, self.mcp)
        if not rows:
            out.append(tr("  Пока нет ни одного. Например:\n"), style="dim")
            out.append("  /mcp add fs npx -y @modelcontextprotocol/server-filesystem .\n"
                       "  /mcp add context7 https://mcp.context7.com/mcp\n")
        marks = {"ok": ("✓", "green"), "error": ("✗", "red"), "off": ("–", "dim"), "starting": ("…", "yellow")}
        for name, state, info, where in rows:
            mark, style = marks[state]
            if state == "ok":
                detail = tr("инструментов: {0} · {1}", info, where)
            elif state == "error":
                detail = info.splitlines()[0][:200] if info else ""
            elif state == "off":
                detail = tr("выключен · {0}", where)
            else:
                detail = tr("подключается… · {0}", where)
            out.append(f"  {mark} ", style=style)
            out.append(f"{name:<14}", style="bold")
            out.append(detail + "\n", style="red" if state == "error" else "dim")
        out.append(tr("\n  /mcp tools [имя] · add · remove · on · off · reload\n  Файл настроек: {0}",
                      (self.config_base or config_dir()) / "settings.json"), style="dim")
        return out

    def _mcp_tools(self, name: str) -> Text:
        servers = [s for s in (self.mcp.servers if self.mcp else []) if not name or s.name == name]
        if not servers:
            return Text(tr("Нет подключённого MCP-сервера «{0}». Список: /mcp", name) if name
                        else tr("Нет подключённых MCP-серверов. Список: /mcp"), style="yellow")
        out = Text()
        for s in servers:
            out.append(f"🔌 {s.name}", style="bold")
            out.append(tr("  инструментов: {0}\n", len(s.tools)), style="dim")
            for t in s.tools:
                desc = " ".join(str(t.get("description") or "").split())
                out.append(f"  {t['name']}", style="bold")
                if desc:
                    out.append("  " + (desc if len(desc) <= 90 else desc[:89] + "…"), style="dim")
                out.append("\n")
        return out

    def _post_notice(self, content: Text) -> None:
        async def go() -> None:
            await self.query_one("#chat", VerticalScroll).mount(Notice(content))
            self._scroll()

        self.run_worker(go())

    def _notify_plain(self, message: str, **kwargs: Any) -> None:
        """notify() without markup: server errors and usage hints contain [brackets]."""
        try:
            self.notify(message, markup=False, **kwargs)
        except TypeError:  # an older Textual without markup=
            self.notify(message.replace("[", "\\["), **kwargs)

    def action_compact(self) -> None:
        if self._agent_busy or not self.agent:
            self.notify(tr("Сначала остановите агента (Esc)."), severity="warning")
            return

        async def go() -> None:
            chat = self.query_one("#chat", VerticalScroll)
            indicator = self.query_one(ActivityIndicator)
            indicator.start()
            indicator.set_phase(tr("Сжимает историю"))
            try:
                result = await self.agent.compact(self.session, force=True)
            except LLMError as e:
                await chat.mount(ErrorMessage(str(e)))
                return
            finally:
                indicator.stop()
            await chat.mount(Notice(self._compacted_text(result) if result else tr("🗜 Сжимать пока нечего.")))
            self._refresh_bars()
            self._scroll()

        self.run_worker(go())

    def action_interrupt(self) -> None:
        if self._agent_busy:
            self.workers.cancel_group(self, "agent")

    def action_toggle_mode(self) -> None:
        if isinstance(self.screen, ModalScreen):
            self.screen.focus_next()
            return
        if self.query_one(MentionList).display:
            self.query_one(PromptInput).accept_suggestion()  # Tab completes the suggestion
            return
        self._agent_mode = "plan" if (self.agent.mode if self.agent else self._agent_mode) == "build" else "build"
        if self.agent:
            self.agent.mode = self._agent_mode  # type: ignore[assignment]
        self._refresh_bars()
        self.notify(tr("Plan: агент только читает и планирует") if self._agent_mode == "plan"
                    else tr("Build: агент может править код"), timeout=2)

    def action_connect(self) -> None:
        if isinstance(self.screen, SetupScreen):
            return

        def done(saved: bool | None) -> None:
            if saved:
                self.auth = load_auth(self.config_base)
                self._build_agent()
                self.session.model = self.settings.model
                self._refresh_bars()
                self.notify(tr('Подключено: {0}', self.settings.model), timeout=3)
            self.query_one(PromptInput).focus()
            if self._pick_project:
                self.call_after_refresh(self.action_projects)

        self.push_screen(SetupScreen(self.settings, self.auth, self.config_base), done)

    async def _reset_chat(self) -> None:
        chat = self.query_one("#chat", VerticalScroll)
        await chat.remove_children()
        if self.agent:
            self.agent.ctx.read_files.clear()
            self.agent._always.clear()

    def action_new_session(self) -> None:
        if self._agent_busy:
            self.notify(tr("Сначала остановите агента (Esc)."), severity="warning")
            return

        async def go() -> None:
            await self._reset_chat()
            await self.query_one("#chat", VerticalScroll).mount(self._welcome())
            self.session = self.store.create(str(self.project_root), self.settings.model)
            self._refresh_bars()

        self.run_worker(go())

    def action_sessions(self) -> None:
        if self._agent_busy:
            self.notify(tr("Сначала остановите агента (Esc)."), severity="warning")
            return
        sessions = [s for s in self.store.list(str(self.project_root)) if s.id != self.session.id]

        def done(session_id: str | None) -> None:
            if session_id:
                self.run_worker(self._open_session(session_id))

        self.push_screen(SessionsScreen(sessions), done)

    async def _open_session(self, session_id: str) -> None:
        loaded = self.store.load(session_id)
        if not loaded:
            return
        await self._reset_chat()
        self.session = loaded
        chat = self.query_one("#chat", VerticalScroll)
        results = {m.get("tool_call_id"): m.get("content", "") for m in loaded.messages if m.get("role") == "tool"}
        tools = self.agent.tools if self.agent else {}
        for m in loaded.messages:
            role = m.get("role")
            if role == "user":
                await chat.mount(UserMessage(str(m.get("content", "")).split("\n\nAttached by the user:")[0]))
            elif role == "assistant":
                if m.get("content"):
                    await chat.mount(AssistantMessage(str(m["content"])))
                for call in m.get("tool_calls") or []:
                    fn = call.get("function", {})
                    tool = tools.get(fn.get("name", ""))
                    try:
                        import json
                        args: dict[str, Any] = json.loads(fn.get("arguments") or "{}")
                    except ValueError:
                        args = {}
                    title = tool.title(args) if tool else fn.get("name", "?")
                    card = ToolCard(fn.get("name", "?"), title, tool.kind if tool else "read")
                    await chat.mount(card)
                    card.finish(ToolResult(str(results.get(call.get("id"), ""))))
        await chat.mount(Notice(tr("↺ Сессия загружена. Продолжайте с того места, где остановились.")))
        self._refresh_bars()
        self._scroll()

    def action_cycle_theme(self) -> None:
        available = getattr(self, "available_themes", {})
        themes = [(name, label) for name, label in THEME_LABELS.items() if name in available]
        if not themes or isinstance(self.screen, ThemeScreen):
            return

        def done(name: str | None) -> None:
            if name:
                self.settings.theme = name
                save_settings(self.settings, self.config_base)
                self.notify(tr('Тема: {0}', THEME_LABELS.get(name, name)), timeout=2)
            self._refresh_bars()

        self.push_screen(ThemeScreen(themes, self.theme), done)

    def action_help(self) -> None:
        self.push_screen(HelpScreen(self._commands()))

    async def on_unmount(self) -> None:
        if self.mcp is not None:
            await self.mcp.close()
        if self._llm_client is not None:
            await self._llm_client.aclose()
        self.store.close()
