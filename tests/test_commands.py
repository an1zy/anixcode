import asyncio
import json

from anixcode import commands
from anixcode.agent import Agent
from anixcode.llm import Completion, ToolCall
from anixcode.settings import Settings
from anixcode.storage import Session


def test_builtin_and_custom_commands(tmp_path):
    folder = tmp_path / ".anixcode" / "commands"
    folder.mkdir(parents=True)
    (folder / "deploy.md").write_text("---\ndescription: выложить на сервер\n---\nDeploy $1 to $2. Notes: $ARGUMENTS", encoding="utf-8")
    (folder / "review.md").write_text("My own review rules: $ARGUMENTS", encoding="utf-8")  # overrides the ready-made one
    (folder / "help.md").write_text("can't override actions", encoding="utf-8")
    (folder / "Bad Name.md").write_text("ignored", encoding="utf-8")
    cmds = commands.all_commands(tmp_path, None)
    deploy = commands.find("deploy", cmds)
    assert deploy.description == "выложить на сервер" and deploy.source == "project" and deploy.takes_args
    assert commands.find("review", cmds).template.startswith("My own")
    assert commands.find("help", cmds).action == "help"
    assert commands.render(deploy.template, "app prod") == "Deploy app to prod. Notes: app prod"
    assert [c.name for c in commands.suggest("de", cmds)][:1] == ["deploy"]
    assert commands.parse("/Fix  TypeError in api") == ("fix", "TypeError in api")


def test_ready_made_prompts_render_cleanly():
    review = commands.find("review", commands.all_commands())
    assert not commands.render(review.template, "").rstrip().endswith("Focus:")
    assert commands.render(review.template, "security").endswith("Focus: security")
    assert "Say hi\n\nloudly" == commands.render("Say hi", "loudly")


def test_memory_and_export(tmp_path):
    commands.append_memory(tmp_path, "используй pnpm")
    commands.append_memory(tmp_path, "тесты через vitest")
    text = (tmp_path / "AGENTS.md").read_text(encoding="utf-8")
    assert text.count("## Заметки") == 1 and "- используй pnpm\n- тесты через vitest" in text
    md = commands.session_markdown("t", "m", [
        {"role": "user", "content": "привет\n\nAttached by the user:\n<file>..."},
        {"role": "assistant", "content": "ок", "tool_calls": [{"function": {"name": "read", "arguments": "{}"}}]},
        {"role": "tool", "content": "out"},
    ])
    assert "привет" in md and "Attached" not in md and "`read`" in md


class FakeClient:
    def __init__(self, script):
        self.script = list(script)

    async def stream_chat(self, model, messages, tools=None):
        yield self.script.pop(0)


def test_undo_then_redo(tmp_path):
    f = tmp_path / "a.txt"
    client = FakeClient([Completion(tool_calls=[ToolCall("w", "write", json.dumps({"path": "a.txt", "content": "v2"}))]),
                         Completion(text="ok")])

    async def allow(_req):
        return "once"

    f.write_text("v1", encoding="utf-8")
    agent = Agent(client, "m", tmp_path, Settings(), ask=allow)
    agent.ctx.mark_read(f)
    session = Session("s", str(tmp_path))

    async def go():
        return [e async for e in agent.run(session, "x")]

    asyncio.run(go())
    assert f.read_text() == "v2"
    agent.undo(session)
    assert f.read_text() == "v1"
    assert agent.redo(session).restored == ["a.txt"] and f.read_text() == "v2"
    assert agent.redo(session) is None
