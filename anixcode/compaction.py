"""Keeping long sessions inside the model's context window.

1. prune: old, big tool outputs are replaced with a short note (cheap, no model call);
2. summarize: if that is not enough, everything except the last turns is summarized by the model.
"""
from __future__ import annotations
from .i18n import tr

import json
from typing import Any

# Rough context sizes by model name (first match wins). Override with settings.context_window.
CONTEXT_HINTS: tuple[tuple[str, int], ...] = (
    ("gemini", 1_000_000), ("gpt-4.1", 1_000_000), ("gpt-5", 400_000), ("claude", 200_000), ("o3", 200_000),
    ("o4", 200_000), ("gpt-4o", 128_000), ("deepseek", 128_000), ("qwen", 128_000), ("llama", 128_000),
    ("mistral", 128_000), ("kimi", 128_000), ("glm", 128_000), ("grok", 128_000),
)
DEFAULT_WINDOW = 64_000
COMPACT_AT = 0.8  # compact when the history reaches 80% of the window
PRUNED = "[old tool output removed to save context; run the tool again if you need it]"
SUMMARY_PREFIX = "[Summary of the earlier conversation]"

SUMMARIZE_PROMPT = """You compress a coding session so the agent can continue it with less context.
Write a concise summary that keeps every specific detail needed to continue:
- the user's goal and requirements, and decisions made;
- files read or changed (exact paths) and what was changed;
- commands run and their important results or errors;
- the current state and what remains to be done.
Use short bullet points. Write in the language the user writes in."""


def context_window(model: str, override: int = 0) -> int:
    if override > 0:
        return override
    low = model.lower()
    return next((size for key, size in CONTEXT_HINTS if key in low), DEFAULT_WINDOW)


def estimate_tokens(messages: list[dict[str, Any]]) -> int:
    """~3 characters per token: deliberately pessimistic (code and Cyrillic tokenize worse than English)."""
    return sum(len(json.dumps(m, ensure_ascii=False)) for m in messages) // 3


def _user_turn_starts(messages: list[dict[str, Any]]) -> list[int]:
    return [i for i, m in enumerate(messages)
            if m.get("role") == "user" and not str(m.get("content", "")).startswith(SUMMARY_PREFIX)]


def prune(messages: list[dict[str, Any]], keep_turns: int = 2, min_chars: int = 1500) -> int:
    """Shrink big tool outputs older than the last ``keep_turns`` turns. Returns characters saved."""
    starts = _user_turn_starts(messages)
    limit = starts[-keep_turns] if len(starts) >= keep_turns else 0
    saved = 0
    for m in messages[:limit]:
        content = m.get("content")
        if m.get("role") == "tool" and isinstance(content, str) and len(content) > min_chars:
            saved += len(content) - len(PRUNED)
            m["content"] = PRUNED
    return saved


def split_point(messages: list[dict[str, Any]], keep_turns: int = 2) -> int:
    """Index where the kept tail starts (always at a user message, so tool calls stay paired)."""
    starts = _user_turn_starts(messages)
    if len(starts) <= keep_turns:
        return starts[-1] if len(starts) > 1 else 0
    return starts[-keep_turns]


def transcript(messages: list[dict[str, Any]], max_chars: int) -> str:
    lines: list[str] = []
    for m in messages:
        role, content = m.get("role"), m.get("content") or ""
        if role == "tool":
            lines.append(f"[tool result] {str(content)[:600]}")
            continue
        if content:
            lines.append(f"[{role}] {content}")
        for call in m.get("tool_calls") or []:
            fn = call.get("function", {})
            lines.append(f"[{role} calls {fn.get('name')}] {str(fn.get('arguments', ''))[:400]}")
    text = "\n".join(lines)
    return text if len(text) <= max_chars else "…(older part cut)…\n" + text[-max_chars:]


def rebuild(summary: str, tail: list[dict[str, Any]]) -> list[dict[str, Any]]:
    return [
        {"role": "user", "content": f"{SUMMARY_PREFIX}\n{summary.strip()}"},
        {"role": "assistant", "content": tr("Понял контекст, продолжаю с того же места.")},
        *tail,
    ]
