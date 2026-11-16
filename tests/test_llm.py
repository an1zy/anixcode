import json

import pytest

from anixcode.llm import ChatStreamParser, LLMError, ReasoningDelta, TextDelta, friendly_error


def sse(obj):
    return "data: " + json.dumps(obj)


def test_text_reasoning_and_usage():
    p = ChatStreamParser()
    events = []
    for obj in ({"choices": [{"delta": {"reasoning_content": "hmm"}}]},
                {"choices": [{"delta": {"content": "Hel"}}]},
                {"choices": [{"delta": {"content": "lo"}, "finish_reason": "stop"}]},
                {"choices": [], "usage": {"prompt_tokens": 10, "completion_tokens": 3}}):
        events += p.feed_line(sse(obj))
    p.feed_line("data: [DONE]")
    r = p.result()
    assert [type(e) for e in events] == [ReasoningDelta, TextDelta, TextDelta]
    assert r.text == "Hello" and r.reasoning == "hmm" and r.usage.completion == 3 and r.finish_reason == "stop"


def test_tool_calls_split_across_chunks():
    p = ChatStreamParser()
    chunks = [
        {"choices": [{"delta": {"tool_calls": [{"index": 0, "id": "c1", "function": {"name": "read", "arguments": ""}}]}}]},
        {"choices": [{"delta": {"tool_calls": [{"index": 0, "function": {"arguments": '{"path": "a'}}]}}]},
        {"choices": [{"delta": {"tool_calls": [{"index": 0, "function": {"arguments": '.py"}'}},
                                               {"index": 1, "id": "c2", "function": {"name": "ls", "arguments": {}}}]}}]},
    ]
    for c in chunks:
        p.feed_line(sse(c))
    calls = p.result().tool_calls
    assert [(c.id, c.name) for c in calls] == [("c1", "read"), ("c2", "ls")]
    assert calls[0].parse_args() == {"path": "a.py"} and calls[1].parse_args() == {}


def test_error_in_stream():
    with pytest.raises(LLMError):
        ChatStreamParser().feed_line(sse({"error": {"message": "quota"}}))


def test_friendly_error():
    assert "API-ключ" in friendly_error(401, '{"error":{"message":"bad key"}}', "x")
    assert "bad key" in friendly_error(401, '{"error":{"message":"bad key"}}', "x")
