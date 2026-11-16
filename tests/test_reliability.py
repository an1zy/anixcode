import asyncio
import json

from anixcode import compaction
from anixcode.agent import Agent, Compacted, Compacting, Done, ToolFinished
from anixcode.llm import Completion, LLMError, TextDelta, ToolCall, is_context_overflow, retry_delay
from anixcode.settings import Settings
from anixcode.storage import Session, Store


class FakeClient:
    def __init__(self, script):
        self.script = list(script)
        self.calls = []

    async def stream_chat(self, model, messages, tools=None):
        self.calls.append([dict(m) for m in messages])
        item = self.script.pop(0)
        if isinstance(item, Exception):
            raise item
        if item.text:
            yield TextDelta(item.text)
        yield item


def run(agent, session, text):
    async def go():
        return [ev async for ev in agent.run(session, text)]
    return asyncio.run(go())


def call(name, **args):
    return ToolCall(f"id_{name}_{len(json.dumps(args))}", name, json.dumps(args))


async def _allow(_req):
    return "once"


def test_retry_policy():
    assert [retry_delay(i) for i in range(4)] == [1.5, 3.0, 6.0, 12.0]
    assert retry_delay(0, "7") == 7.0 and retry_delay(0, "999") == 30.0 and retry_delay(5) == 30.0
    assert is_context_overflow(LLMError("x", 400, "This model's maximum context length is 128000 tokens"))
    assert not is_context_overflow(LLMError("x", 401, "maximum context length"))


def test_undo_restores_edits_and_deletes_created_files(tmp_path):
    (tmp_path / "a.py").write_text("old\n")
    store = Store(tmp_path / "s.db")
    session = store.create(str(tmp_path))
    client = FakeClient([
        Completion(tool_calls=[call("read", path="a.py")]),
        Completion(tool_calls=[call("edit", path="a.py", old_string="old", new_string="new"),
                               call("write", path="b.py", content="x")]),
        Completion(text="done"),
        Completion(tool_calls=[call("write", path="c.py", content="y")]),
        Completion(text="done 2"),
    ])
    agent = Agent(client, "m", tmp_path, Settings(), ask=_allow)
    run(agent, session, "first")
    run(agent, session, "second")
    assert (tmp_path / "c.py").exists()
    r = agent.undo(session)  # undoes turn 2 only
    assert r.deleted == ["c.py"] and not (tmp_path / "c.py").exists() and (tmp_path / "b.py").exists()
    # survives a restart: a new agent over the same store undoes turn 1
    agent2 = Agent(FakeClient([]), "m", tmp_path, Settings())
    r = agent2.undo(store.load(session.id))
    assert sorted(r.restored) == ["a.py"] and r.deleted == ["b.py"]
    assert (tmp_path / "a.py").read_text() == "old\n" and not (tmp_path / "b.py").exists()
    assert agent2.undo(store.load(session.id)) is None
    assert "undid" in session.messages[-1]["content"]


def test_prune_and_split_keep_tool_pairs():
    msgs = [
        {"role": "user", "content": "one"},
        {"role": "assistant", "content": None, "tool_calls": [{"id": "1", "type": "function",
                                                                "function": {"name": "read", "arguments": "{}"}}]},
        {"role": "tool", "tool_call_id": "1", "content": "x" * 5000},
        {"role": "assistant", "content": "ok"},
        {"role": "user", "content": "two"},
        {"role": "assistant", "content": "ok2"},
        {"role": "user", "content": "three"},
        {"role": "tool", "tool_call_id": "2", "content": "y" * 5000},
    ]
    saved = compaction.prune(msgs)
    assert saved > 4000 and msgs[2]["content"] == compaction.PRUNED and len(msgs[7]["content"]) == 5000
    cut = compaction.split_point(msgs)
    assert msgs[cut]["content"] == "two"
    rebuilt = compaction.rebuild("summary here", msgs[cut:])
    assert rebuilt[0]["content"].startswith(compaction.SUMMARY_PREFIX) and rebuilt[2]["content"] == "two"


def test_auto_compaction_summarizes_old_turns(tmp_path):
    store = Store(tmp_path / "s.db")
    session = store.create(str(tmp_path))
    for i in range(6):
        session.add({"role": "user", "content": f"task {i} " + "z" * 3000})
        session.add({"role": "assistant", "content": "answer " + "w" * 3000})
    client = FakeClient([Completion(text="- did tasks 0-5"), Completion(text="final")])
    agent = Agent(client, "m", tmp_path, Settings(context_window=8000))
    events = run(agent, session, "next task")
    kinds = [type(e) for e in events]
    assert Compacting in kinds and Compacted in kinds and isinstance(events[-1], Done)
    compacted = next(e for e in events if isinstance(e, Compacted))
    assert compacted.summarized and compacted.after < compacted.before
    assert session.messages[0]["content"].startswith(compaction.SUMMARY_PREFIX)
    assert store.load(session.id).messages[0]["content"].startswith(compaction.SUMMARY_PREFIX)
    # the request after compaction carried the summary, not the old turns
    assert "did tasks 0-5" in client.calls[-1][1]["content"]


def test_context_overflow_error_triggers_compaction_and_retry(tmp_path):
    session = Session(id="s", cwd=str(tmp_path))
    for i in range(3):
        session.add({"role": "user", "content": f"q{i}"})
        session.add({"role": "assistant", "content": f"a{i}"})
    overflow = LLMError("too big", 400, "maximum context length exceeded")
    client = FakeClient([overflow, Completion(text="summary"), Completion(text="ok after compaction")])
    events = run(Agent(client, "m", tmp_path, Settings()), session, "go")
    assert any(isinstance(e, Compacted) for e in events) and isinstance(events[-1], Done)
    assert session.messages[-1]["content"] == "ok after compaction"


def test_tools_after_compaction_still_work(tmp_path):
    (tmp_path / "f.txt").write_text("hi")
    session = Session(id="s", cwd=str(tmp_path))
    client = FakeClient([Completion(tool_calls=[call("read", path="f.txt")]), Completion(text="ok")])
    events = run(Agent(client, "m", tmp_path, Settings()), session, "read it")
    assert [e for e in events if isinstance(e, ToolFinished)][0].result.error is False
