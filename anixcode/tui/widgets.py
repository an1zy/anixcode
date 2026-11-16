"""Chat widgets: messages, streaming markdown, tool cards, prompt input."""
from __future__ import annotations
from ..i18n import tr

import re

from rich.console import Group
from rich.syntax import Syntax
from rich.text import Text
from textual import events
from textual.containers import Horizontal, Vertical, VerticalScroll
from textual.message import Message
from textual.widgets import Markdown, OptionList, Static, TextArea

from ..mentions import MENTION
from ..tools import ToolResult

_PREFIX = re.compile(r"(?:^|\s)@([^\s@]*)$")
_CMD_PREFIX = re.compile(r"^/([a-z0-9_-]*)$", re.IGNORECASE)

TOOL_ICONS = {"read": "→", "ls": "▤", "glob": "⌕", "grep": "⌕", "edit": "✎", "write": "✎", "bash": "$"}


class Chip(Static):
    """A small clickable label (looks like a tag, takes one line)."""

    def __init__(self, label: str, on_press, **kwargs) -> None:
        super().__init__(label, **kwargs)
        self._on_press = on_press

    def on_click(self) -> None:
        self._on_press()


class PermissionBar(Horizontal):
    """One line above the prompt: "Выполнить: rm -rf build   [Разрешить ⏎] [Отклонить esc] [Полный доступ]"."""

    def __init__(self, on_decide, **kwargs) -> None:
        super().__init__(**kwargs)
        self.perm_text = Static(id="perm-text")
        self.perm_decide = on_decide
        self.perm_ok = Chip(tr(" Разрешить ⏎ "), lambda: self.perm_decide("once"), classes="chip ok")
        self.perm_no = Chip(tr(" Отклонить esc "), lambda: self.perm_decide("deny"), classes="chip no")
        self.perm_full = Chip(tr(" Полный доступ "), lambda: self.perm_decide("full"), classes="chip full")

    def compose(self):
        yield self.perm_text
        yield self.perm_ok
        yield self.perm_no
        yield self.perm_full

    def show(self, req) -> None:
        text = Text()
        verb = tr("Изменить") if req.kind == "edit" else tr("Выполнить")
        text.append(f"{verb}: ", style="bold")
        text.append(req.title, style="bold red" if req.dangerous else "bold")
        for why in req.danger:
            text.append(f"  · {why}", style="red")
        for why in req.warnings:
            text.append(f"  · {why}", style="yellow")
        text.append(tr("\nEnter — разрешить · Esc — отклонить · напишите текст и Enter — отклонить с пояснением"),
                    style="dim")
        self.perm_text.update(text)
        self.set_class(req.dangerous, "danger")
        self.display = True

    def hide(self) -> None:
        self.display = False


class MentionList(OptionList):
    """@file suggestions under the cursor. Never takes focus: the prompt keeps typing."""

    can_focus = False

    def __init__(self, **kwargs) -> None:
        super().__init__(**kwargs)
        self.mention_items: list[str] = []
        self.takes_args: dict[str, bool] = {}  # for /commands: does it expect arguments?

    def show(self, items: list[str], labels: list[Text] | None = None, takes_args: dict[str, bool] | None = None) -> None:
        self.mention_items = items
        self.takes_args = takes_args or {}
        self.clear_options()
        self.add_options(labels or [Text(("📁 " if i.endswith("/") else "📄 ") + i) for i in items])
        self.highlighted = 0
        self.display = True

    def hide(self) -> None:
        self.display = False

    def current(self) -> str | None:
        if not self.display or self.highlighted is None or not self.mention_items:
            return None
        return self.mention_items[self.highlighted]


class PromptInput(TextArea):
    """Enter sends, Ctrl+J (or Shift+Enter where the terminal supports it) adds a new line."""

    class Submitted(Message):
        def __init__(self, text: str) -> None:
            super().__init__()
            self.text = text

    def __init__(self, **kwargs) -> None:
        super().__init__(soft_wrap=True, show_line_numbers=False, tab_behavior="focus", **kwargs)
        self._prompt_history: list[str] = []
        self._prompt_pos = 0
        self.mention_list: MentionList | None = None
        self.permission_handler = None  # set while the agent waits for approval

    def suggestion_query(self) -> tuple[str, str] | None:
        """('command', 'rev') while typing '/rev' at the start, ('mention', 'src/a') after '@', else None."""
        row, col = self.cursor_location
        line = self.document.get_line(row)[:col]
        if row == 0:
            m = _CMD_PREFIX.match(line)
            if m:
                return "command", m.group(1)
        m = _PREFIX.search(line)
        return ("mention", m.group(1)) if m else None

    def accept_suggestion(self, item: str | None = None, submit: bool = False) -> None:
        ml = self.mention_list
        item = item or (ml.current() if ml else None)
        query = self.suggestion_query()
        if item is None or query is None:
            if ml:
                ml.hide()
            return
        kind, prefix = query
        row, col = self.cursor_location
        if kind == "command":
            needs_args = ml.takes_args.get(item, False) if ml else False
            if ml:
                ml.hide()
            if submit and not needs_args:
                self.clear()
                self.post_message(self.Submitted(f"/{item}"))
            else:
                self.replace(f"/{item} ", (0, 0), (row, col))
            return
        self.replace(f"@{item}" if item.endswith("/") else f"@{item} ", (row, col - len(prefix) - 1), (row, col))
        if ml and not item.endswith("/"):
            ml.hide()

    async def _on_key(self, event: events.Key) -> None:
        handler = self.permission_handler
        if handler is not None and event.key in ("enter", "escape"):
            event.stop()
            event.prevent_default()
            typed = self.text.strip()
            if event.key == "escape":
                handler("deny", "")
            elif typed:
                self.clear()
                handler("deny", typed)  # "no, do it like this instead"
            else:
                handler("once", "")
            return
        ml = self.mention_list
        if ml is not None and ml.display:
            if event.key in ("up", "down", "enter", "tab", "escape"):
                event.stop()
                event.prevent_default()
                if event.key == "up":
                    ml.action_cursor_up()
                elif event.key == "down":
                    ml.action_cursor_down()
                elif event.key == "escape":
                    ml.hide()
                else:
                    self.accept_suggestion(submit=event.key == "enter")
                return
        if event.key == "enter":
            event.stop()
            event.prevent_default()
            text = self.text.strip()
            if text:
                self._prompt_history.append(text)
                self._prompt_pos = len(self._prompt_history)
                self.post_message(self.Submitted(text))
                self.clear()
            return
        if event.key in ("ctrl+j", "shift+enter"):
            event.stop()
            event.prevent_default()
            self.insert("\n")
            return
        if event.key in ("up", "down") and "\n" not in self.text and self._prompt_history:
            event.stop()
            event.prevent_default()
            step = -1 if event.key == "up" else 1
            self._prompt_pos = max(0, min(len(self._prompt_history), self._prompt_pos + step))
            self.text = self._prompt_history[self._prompt_pos] if self._prompt_pos < len(self._prompt_history) else ""
            self.move_cursor(self.document.end)
            return
        await super()._on_key(event)


class UserMessage(Static):
    def __init__(self, text: str) -> None:
        rich = Text(text)
        for m in MENTION.finditer(text):
            rich.stylize("bold underline", m.start(), m.end())
        super().__init__(rich)


class Notice(Static):
    pass


class ErrorMessage(Static):
    def __init__(self, text: str) -> None:
        super().__init__(Text("✗ " + text))


class AssistantMessage(Markdown):
    """Streaming Markdown.

    New Textual versions can append to a Markdown widget (only the new part is parsed); older ones
    re-render the whole text, so there the refresh rate drops as the answer grows.
    """

    def __init__(self, text: str = "") -> None:
        super().__init__(text)
        self._stream_text = text
        self._stream_pending = ""
        self._stream_busy = False
        self._stream_last = 0.0
        self._stream_timer = None
        self._can_append = hasattr(Markdown, "append")

    def on_mount(self) -> None:
        self._stream_timer = self.set_interval(0.05, self._flush)

    def append_text(self, chunk: str) -> None:
        self._stream_text += chunk
        self._stream_pending += chunk

    def _interval(self) -> float:
        if self._can_append:
            return 0.05
        n = len(self._stream_text)
        return 0.1 if n < 3000 else 0.25 if n < 12000 else 0.6

    async def _flush(self, force: bool = False) -> None:
        import time

        if self._stream_busy or not self._stream_pending:
            return
        now = time.monotonic()
        if not force and now - self._stream_last < self._interval():
            return
        self._stream_busy = True
        self._stream_last = now
        chunk, self._stream_pending = self._stream_pending, ""
        try:
            if self._can_append:
                try:
                    await self.append(chunk)
                    return
                except Exception:
                    self._can_append = False  # fall back to full re-render
            await self.update(self._stream_text)
        finally:
            self._stream_busy = False

    async def finish(self) -> None:
        if self._stream_timer is not None:
            self._stream_timer.stop()
        while self._stream_busy:
            import asyncio
            await asyncio.sleep(0.01)
        await self._flush(force=True)


class ReasoningBlock(Static):
    """Model thinking: shows the tail while streaming, collapses when done (click to expand)."""

    def __init__(self) -> None:
        super().__init__("")
        self._think_text = ""
        self._think_done = False
        self._think_open = False

    def append(self, chunk: str) -> None:
        self._think_text += chunk
        tail = " ".join(self._think_text[-300:].split())
        self.update(Text(f"💭 {tail}"))

    def finish(self) -> None:
        self._think_done = True
        self._render_done()

    def _render_done(self) -> None:
        if self._think_open:
            self.update(Text(tr('💭 Размышления (свернуть)\n{0}', self._think_text.strip())))
        else:
            self.update(Text(tr('💭 Размышления · {0} символов (клик, чтобы развернуть)', len(self._think_text))))

    def on_click(self) -> None:
        if self._think_done:
            self._think_open = not self._think_open
            self._render_done()


class ToolCard(Vertical):
    """One tool call: a header line, and a body (diff or output) that opens on click."""

    def __init__(self, tool: str, title: str, kind: str, full_title: str = "", dangerous: bool = False) -> None:
        super().__init__(classes=f"kind-{kind}" + (" danger" if dangerous else ""))
        self.tool = tool
        one_line = " ".join(title.split())
        self.title_text = one_line if len(one_line) <= 100 else one_line[:99] + "…"
        self.full_title = full_title or title
        self.tool_header = Static(classes="tool-header")
        self.tool_body = VerticalScroll(classes="tool-body")
        self.tool_output = Static()
        self._set_header("◌", "")

    def compose(self):
        yield self.tool_header
        with self.tool_body:
            yield self.tool_output

    def _set_header(self, mark: str, extra: str | Text) -> None:
        icon = TOOL_ICONS.get(self.tool, "◇" if self.tool.startswith("mcp__") else "•")
        line = Text.assemble((f"{mark} ", "bold"), (f"{icon} ", "bold"), (self.title_text, ""))
        if extra:
            line.append("  ")
            line.append(extra if isinstance(extra, Text) else Text(extra, style="dim"))
        self.tool_header.update(line)

    def show_pending(self, preview: str | None, kind: str) -> None:
        """Waiting for approval: show what is about to happen."""
        self.add_class("pending")
        self._set_header("?", Text(tr("ждёт разрешения"), style="yellow"))
        if preview:
            if kind == "edit":
                self.tool_output.update(Syntax(preview.rstrip(), "diff", theme="monokai", word_wrap=True,
                                               background_color="default"))
            elif kind == "mcp":
                self.tool_output.update(Syntax(preview.rstrip(), "json", theme="monokai", word_wrap=True,
                                               background_color="default"))
            else:
                self.tool_output.update(Text(f"$ {preview}", style="bold"))
            self.add_class("open")

    def finish(self, result: ToolResult) -> None:
        self.remove_class("pending", "open")  # collapse after approval; small diffs and errors reopen below
        self.add_class("error" if result.error else "done")
        if result.diff is not None:
            extra = Text.assemble((f"+{result.added}", "green"), " ", (f"−{result.removed}", "red"))
        else:
            extra = Text(result.title or "", style="dim") if result.title and result.title != self.title_text else ""
        self._set_header("✗" if result.error else "✓", extra)
        if result.diff:
            self.tool_output.update(Syntax(result.diff.rstrip(), "diff", theme="monokai", word_wrap=True,
                                            background_color="default"))
            if result.added + result.removed <= 30:
                self.add_class("open")  # small diffs are shown right away
        elif result.output and (self.tool in ("bash", "grep", "glob", "ls") or self.tool.startswith("mcp__")) \
                or result.error:
            out = result.output.strip()
            lines = out.splitlines()
            shown = "\n".join(lines[:200]) + (tr('\n… ещё {0} строк', len(lines) - 200) if len(lines) > 200 else "")
            parts = []
            if self.tool == "bash" and self.full_title != self.title_text:
                parts.append(Text(f"$ {self.full_title}\n", style="bold"))
            parts.append(Text(shown, style="red" if result.error else "dim"))
            self.tool_output.update(Group(*parts))
            if result.error:
                self.add_class("open")

    def on_click(self) -> None:
        self.toggle_class("open")
