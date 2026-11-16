import asyncio
import json

from anixcode.agent import Agent, AgentError, Done, PermissionRequest, ToolFinished, ToolStarted
from anixcode.llm import Completion, LLMError, TextDelta, ToolCall, Usage
from anixcode.settings import Settings
from anixcode.storage import Store


class FakeClient:
    """Replays scripted completions and records what the agent sent."""

    def __init__(self, script):
        self.script = list(script)
        self.calls = []

    async def stream_chat(self, model, messages, tools=None):
        self.calls.append({"messages": [dict(m) for m in messages], "tools": [t["function"]["name"] for t in tools or []]})
        item = self.script.pop(0)
        if isinstance(item, Exception):
            raise item
        if item.text:
            yield TextDelta(item.text)
        yield item


def call(name, **args):
    return ToolCall(f"id_{name}", name, json.dumps(args))


def collect(agent, session, text):
    async def go():
        return [ev async for ev in agent.run(session, text)]
    return asyncio.run(go())


def test_tool_loop_edits_file_with_permission(tmp_path):
    (tmp_path / "app.py").write_text("print('hi')\n")
    client = FakeClient([
        Completion(tool_calls=[call("read", path="app.py")]),
        Completion(tool_calls=[call("edit", path="app.py", old_string="hi", new_string="hello")]),
        Completion(text="Готово", usage=Usage(100, 5)),
    ])
    asked = []

    async def ask(req: PermissionRequest):
        asked.append(req)
        return "once"

    store = Store(tmp_path / "s.db")
    session = store.create(str(tmp_path))
    events = collect(Agent(client, "m", tmp_path, Settings(), ask=ask), session, "поменяй приветствие")
    assert (tmp_path / "app.py").read_text() == "print('hello')\n"
    assert len(asked) == 1 and asked[0].kind == "edit" and "+print('hello')" in asked[0].preview
    assert isinstance(events[-1], Done) and session.tokens_out == 5
    # the history the model saw is valid: every tool call got an answer
    last = client.calls[-1]["messages"]
    assert [m["role"] for m in last] == ["system", "user", "assistant", "tool", "assistant", "tool"]
    # the session was persisted
    loaded = store.load(session.id)
    assert loaded.title == "поменяй приветствие" and len(loaded.messages) == 6


def test_denied_and_plan_mode(tmp_path):
    client = FakeClient([Completion(tool_calls=[call("bash", command="rm -rf /")]), Completion(text="ok")])
    events = collect(Agent(client, "m", tmp_path), tmp_path and _session(tmp_path), "x")
    finished = [e for e in events if isinstance(e, ToolFinished)]
    assert finished[0].result.error and "denied" in finished[0].result.output

    client = FakeClient([Completion(tool_calls=[call("write", path="a", content="b")]), Completion(text="plan")])
    agent = Agent(client, "m", tmp_path)
    agent.mode = "plan"
    events = collect(agent, _session(tmp_path), "x")
    assert "Plan mode" in [e for e in events if isinstance(e, ToolFinished)][0].result.output
    assert "write" not in client.calls[0]["tools"] and "read" in client.calls[0]["tools"]
    assert not (tmp_path / "a").exists()


def test_always_allow_and_errors(tmp_path):
    client = FakeClient([
        Completion(tool_calls=[call("write", path="a.txt", content="1"), call("write", path="b.txt", content="2")]),
        Completion(tool_calls=[ToolCall("bad", "read", "{not json"), ToolCall("u", "nope", "{}")]),
        LLMError("401"),
    ])
    asked = []

    async def ask(req):
        asked.append(req)
        return "always"

    events = collect(Agent(client, "m", tmp_path, ask=ask), _session(tmp_path), "x")
    assert len(asked) == 1 and (tmp_path / "b.txt").read_text() == "2"
    outputs = [e.result.output for e in events if isinstance(e, ToolFinished)]
    assert any("Invalid JSON" in o for o in outputs) and any("Unknown tool" in o for o in outputs)
    assert isinstance(events[-1], AgentError)
    assert sum(isinstance(e, ToolStarted) for e in events) == 4


def _session(tmp_path):
    from anixcode.storage import Session
    return Session(id="t", cwd=str(tmp_path))
