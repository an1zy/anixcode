"""The agent loop: model -> tool calls -> results -> model … until it answers."""
from __future__ import annotations
from .i18n import tr

import asyncio
import json
import time
from collections.abc import AsyncIterator, Awaitable, Callable
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Literal, Protocol

from . import compaction
from .airpc import AirpcReporter
from .checkpoints import Checkpoints
from .danger import check_command
from .mentions import expand_mentions
from .llm import Completion, LLMError, ReasoningDelta, Retrying, TextDelta, ToolCall, is_context_overflow
from .prompts import system_prompt
from .settings import Settings
from .storage import Session
from .tools import Tool, ToolContext, ToolError, ToolResult, default_tools

Decision = Literal["once", "always", "deny"]


class ChatClient(Protocol):
    def stream_chat(self, model: str, messages: list[dict[str, Any]], tools: list[dict[str, Any]] | None = None
                    ) -> AsyncIterator[TextDelta | ReasoningDelta | Completion]: ...


@dataclass
class PermissionRequest:
    tool: str
    kind: str
    title: str
    preview: str | None  # diff or command
    danger: list[str] = field(default_factory=list)  # destructive: highlighted in red
    warnings: list[str] = field(default_factory=list)
    call_id: str = ""
    feedback: str = ""  # what the user typed instead of approving (sent to the model)

    @property
    def dangerous(self) -> bool:
        return bool(self.danger)


@dataclass
class ToolStarted:
    call_id: str
    tool: str
    title: str
    kind: str
    dangerous: bool = False


@dataclass
class Attached:
    paths: list[str]


@dataclass
class ToolFinished:
    call_id: str
    tool: str
    result: ToolResult


@dataclass
class StepFinished:
    tokens_in: int
    tokens_out: int


@dataclass
class Done:
    pass


@dataclass
class AgentError:
    message: str


@dataclass
class Compacting:
    pass


@dataclass
class Compacted:
    before: int  # estimated tokens
    after: int
    summarized: bool


@dataclass
class UndoResult:
    restored: list[str]
    deleted: list[str]


AgentEvent = (Attached | TextDelta | ReasoningDelta | Retrying | ToolStarted | ToolFinished | StepFinished | Done | AgentError
              | Compacting | Compacted)
AskPermission = Callable[[PermissionRequest], Awaitable[Decision]]


async def _deny_all(_req: PermissionRequest) -> Decision:
    return "deny"


class Agent:
    def __init__(self, client: ChatClient, model: str, root: Path, settings: Settings | None = None,
                 ask: AskPermission | None = None, tools: dict[str, Tool] | None = None,
                 reporter: AirpcReporter | None = None, provider: str = "") -> None:
        self.client = client
        self.model = model
        self.provider = provider
        self.settings = settings or Settings()
        self.ask = ask or _deny_all
        self.tools = tools or default_tools()
        self.ctx = ToolContext(root.resolve())
        self.mode: Literal["build", "plan"] = "build"
        self._always: set[str] = set()  # kinds approved for this session
        self.reporter = reporter or AirpcReporter(enabled=False)
        self._checkpoints: dict[str, Checkpoints] = {}
        self.last_args: dict[str, Any] = {}  # arguments of the tool call being started (for the UI)

    # -- context window --------------------------------------------------------------
    @property
    def window(self) -> int:
        return compaction.context_window(self.model, self.settings.context_window)

    def context_usage(self, session: Session) -> float:
        """0..1: how full the context is (estimate)."""
        return min(1.0, compaction.estimate_tokens(session.messages) / self.window)

    async def compact(self, session: Session, force: bool = False) -> Compacted | None:
        """Prune old tool outputs; summarize old turns if still too big (or if forced)."""
        before = compaction.estimate_tokens(session.messages)
        compaction.prune(session.messages)
        after = compaction.estimate_tokens(session.messages)
        summarized = False
        if force or after > self.window * compaction.COMPACT_AT * 0.75:
            cut = compaction.split_point(session.messages)
            if cut > 0:
                head, tail = session.messages[:cut], session.messages[cut:]
                summary = await self._summarize(head)
                session.messages[:] = compaction.rebuild(summary, tail)
                summarized = True
                after = compaction.estimate_tokens(session.messages)
        if after == before:
            return None
        if session.store:
            session.store.replace_messages(session)
        return Compacted(before, after, summarized)

    async def _summarize(self, head: list[dict[str, Any]]) -> str:
        text = compaction.transcript(head, max_chars=int(self.window * 3 * 0.6))
        messages = [{"role": "system", "content": compaction.SUMMARIZE_PROMPT},
                    {"role": "user", "content": f"Conversation to summarize:\n\n{text}"}]
        result = ""
        async for ev in self.client.stream_chat(self.model, messages, None):
            if isinstance(ev, Completion):
                result = ev.text
        if not result.strip():
            raise LLMError(tr("Не удалось сжать историю: модель вернула пустую сводку."))
        return result

    def redo(self, session: Session) -> UndoResult | None:
        done = self.checkpoints(session).redo_last()
        if done is None:
            return None
        restored, deleted = done
        for p in (*restored, *deleted):
            self.ctx.read_files.pop(p, None)
        rel = [self.ctx.rel(p) for p in restored], [self.ctx.rel(p) for p in deleted]
        session.add({"role": "user", "content": "[The user re-applied the file changes they had undone: "
                                                f"{', '.join(rel[0] + rel[1])}. Re-read them before editing.]"})
        return UndoResult(*rel)

    # -- undo ------------------------------------------------------------------------
    def checkpoints(self, session: Session) -> Checkpoints:
        if session.id not in self._checkpoints:
            self._checkpoints[session.id] = Checkpoints(session)
        return self._checkpoints[session.id]

    def undo(self, session: Session) -> UndoResult | None:
        """Put back the files changed by the latest agent turn that changed files."""
        done = self.checkpoints(session).undo_last()
        if done is None:
            return None
        restored, deleted = done
        for p in (*restored, *deleted):
            self.ctx.read_files.pop(p, None)  # force a fresh read before the next edit
        rel = [self.ctx.rel(p) for p in restored], [self.ctx.rel(p) for p in deleted]
        note = ("[The user undid the file changes from your previous turn. These files are back to their "
                f"earlier state: {', '.join(rel[0] + rel[1])}. Re-read them before editing again.]")
        session.add({"role": "user", "content": note})
        return UndoResult(*rel)

    def tool_schemas(self) -> list[dict[str, Any]]:
        return [t.schema() for t in self.tools.values() if self.mode == "build" or t.kind == "read"]

    async def run(self, session: Session, user_text: str) -> AsyncIterator[AgentEvent]:
        content, attached = expand_mentions(user_text, self.ctx)
        session.add({"role": "user", "content": content})
        if attached:
            yield Attached(attached)
        cps = self.checkpoints(session)
        turn = cps.new_turn()
        self.ctx.before_write = lambda p: cps.snapshot(turn, p)
        self.reporter.send("start", model=self.model, provider=self.provider)
        try:
            async for event in self._loop(session):
                yield event
        finally:
            self.reporter.send("end", model=self.model, tokens=session.tokens_out or None)

    async def _loop(self, session: Session) -> AsyncIterator[AgentEvent]:
        system = {"role": "system", "content": system_prompt(self.ctx.root, self.mode)}
        overflow_retried = False
        gave_up = False  # compaction could not shrink the history: don't retry every step
        for _step in range(self.settings.max_steps):
            limit = self.window * compaction.COMPACT_AT
            if not gave_up and compaction.estimate_tokens(session.messages) > limit:
                yield Compacting()
                try:
                    result = await self.compact(session)
                except LLMError as e:
                    yield AgentError(str(e))
                    return
                if result:
                    yield result
                if result is None or result.after > limit:
                    gave_up = True
            messages = [system, *session.messages]
            completion: Completion | None = None
            text_so_far: list[str] = []
            last_report = 0.0
            try:
                async for ev in self.client.stream_chat(self.model, messages, self.tool_schemas()):
                    if isinstance(ev, Completion):
                        completion = ev
                        continue
                    if isinstance(ev, TextDelta):
                        text_so_far.append(ev.text)
                    if time.monotonic() - last_report > 2:
                        last_report = time.monotonic()
                        approx = sum(map(len, text_so_far)) // 4
                        self.reporter.send("progress", model=self.model, tokens=session.tokens_out + approx)
                    yield ev
            except LLMError as e:
                if is_context_overflow(e) and not overflow_retried and not text_so_far:
                    overflow_retried = True  # the window guess was too big: compact hard and retry once
                    yield Compacting()
                    try:
                        result = await self.compact(session, force=True)
                    except LLMError as e2:
                        yield AgentError(str(e2))
                        return
                    if result:
                        yield result
                        continue
                yield AgentError(str(e))
                return
            except asyncio.CancelledError:
                if text_so_far:
                    session.add({"role": "assistant", "content": "".join(text_so_far) + "\n\n[interrupted]"})
                raise
            if completion is None:
                yield AgentError(tr("Модель ничего не ответила."))
                return
            if completion.usage:
                session.tokens_in += completion.usage.prompt
                session.tokens_out += completion.usage.completion
            message: dict[str, Any] = {"role": "assistant", "content": completion.text or None}
            if completion.tool_calls:
                message["tool_calls"] = [
                    {"id": c.id, "type": "function", "function": {"name": c.name, "arguments": c.arguments or "{}"}}
                    for c in completion.tool_calls
                ]
            elif not completion.text:
                message["content"] = ""
            session.add(message)
            if session.store:
                session.store.touch(session)
            yield StepFinished(session.tokens_in, session.tokens_out)
            if not completion.tool_calls:
                yield Done()
                return

            pending = [c.id for c in completion.tool_calls]
            try:
                for call in completion.tool_calls:
                    async for ev in self._execute(call, session):
                        yield ev
                    pending.remove(call.id)
            finally:
                # Every tool call needs an answer, or the next request is rejected by the API.
                for call_id in pending:
                    session.add({"role": "tool", "tool_call_id": call_id, "content": "Cancelled by the user."})
        yield AgentError(tr('Агент сделал {0} шагов и остановился. Напишите «продолжай».', self.settings.max_steps))

    async def _execute(self, call: ToolCall, session: Session) -> AsyncIterator[AgentEvent]:
        tool = self.tools.get(call.name)
        try:
            args = call.parse_args()
        except (ValueError, json.JSONDecodeError) as e:
            args, bad_args = {}, str(e)
        else:
            bad_args = ""
        title = tool.title(args) if tool else call.name
        self.last_args = args
        risk = check_command(str(args.get("command", "")), self.ctx.root) if call.name == "bash" else None
        yield ToolStarted(call.id, call.name, title, tool.kind if tool else "read", bool(risk and risk.dangerous))
        if tool is not None:
            self.reporter.send("tool", model=self.model)
        result = await self._run_tool(tool, call, args, bad_args, risk)
        session.add({"role": "tool", "tool_call_id": call.id, "content": result.output})
        yield ToolFinished(call.id, call.name, result)

    async def _run_tool(self, tool: Tool | None, call: ToolCall, args: dict[str, Any], bad_args: str,
                        risk: Any = None) -> ToolResult:
        if tool is None:
            return ToolResult(f"Unknown tool '{call.name}'. Available: {', '.join(self.tools)}", error=True)
        if bad_args:
            return ToolResult(f"Invalid JSON arguments: {bad_args}", error=True)
        if tool.kind != "read":
            if self.mode == "plan":
                return ToolResult("Not allowed in Plan mode. Describe the change instead; the user will switch "
                                  "to Build mode.", error=True)
            policy = "allow" if self.settings.full_access else self.settings.permissions.get(tool.kind, "ask")
            if policy == "deny":
                return ToolResult("This action is disabled in the settings.", error=True)
            key = tool.name if tool.kind == "mcp" else tool.kind  # MCP: approve one tool, not every server
            if policy == "ask" and key not in self._always:
                try:
                    preview = tool.preview(args, self.ctx)
                except Exception:
                    preview = None
                req = PermissionRequest(tool.name, tool.kind, tool.title(args), preview,
                                        danger=list(risk.danger) if risk else [],
                                        warnings=list(risk.warnings) if risk else [], call_id=call.id)
                decision = await self.ask(req)
                if decision == "deny":
                    if req.feedback:
                        return ToolResult(f"The user denied this action and said: {req.feedback}", error=True)
                    return ToolResult("The user denied this action. Ask what they want instead.", error=True)
                if decision == "always":
                    self._always.add(key)
        try:
            return await tool.run(args, self.ctx)
        except ToolError as e:
            return ToolResult(f"Error: {e}", title=tool.title(args), error=True)
        except asyncio.CancelledError:
            raise
        except Exception as e:  # a bug in a tool must not kill the session
            return ToolResult(f"Error: {type(e).__name__}: {e}", title=tool.title(args), error=True)
