"""OpenAI-compatible chat client with streaming and tool calls."""
from __future__ import annotations
from .i18n import tr

import json
import uuid
from collections.abc import AsyncIterator
from dataclasses import dataclass, field
from typing import Any


class LLMError(Exception):
    """A readable error for the user (bad key, no money, model not found, …)."""

    def __init__(self, message: str, status: int | None = None, raw: str = "") -> None:
        super().__init__(message)
        self.status = status
        self.raw = raw


RETRY_STATUSES = frozenset({408, 409, 425, 429, 500, 502, 503, 504, 520, 522, 524, 529})
MAX_ATTEMPTS = 5
_OVERFLOW_HINTS = ("context length", "context_length", "maximum context", "context window", "too many tokens",
                   "prompt is too long", "reduce the length", "token limit", "input is too long", "too long")


def retry_delay(attempt: int, retry_after: str | None = None) -> float:
    """Seconds to wait before retry number ``attempt`` (0-based): honours Retry-After, else 1.5, 3, 6, 12 s."""
    if retry_after:
        try:
            return min(30.0, max(0.5, float(retry_after)))
        except ValueError:
            pass
    return min(30.0, 1.5 * 2 ** attempt)


def is_context_overflow(error: LLMError) -> bool:
    text = f"{error} {error.raw}".lower()
    return error.status in (400, 413, 422) and any(h in text for h in _OVERFLOW_HINTS)


@dataclass
class Usage:
    prompt: int = 0
    completion: int = 0


@dataclass
class TextDelta:
    text: str


@dataclass
class ReasoningDelta:
    text: str


@dataclass
class Retrying:
    attempt: int
    max_attempts: int
    delay: float
    reason: str


@dataclass
class ToolCall:
    id: str
    name: str
    arguments: str  # raw JSON text as the model wrote it

    def parse_args(self) -> dict[str, Any]:
        if not self.arguments.strip():
            return {}
        data = json.loads(self.arguments)
        if not isinstance(data, dict):
            raise ValueError("arguments must be a JSON object")
        return data


@dataclass
class Completion:
    text: str = ""
    reasoning: str = ""
    tool_calls: list[ToolCall] = field(default_factory=list)
    finish_reason: str | None = None
    usage: Usage | None = None


class ChatStreamParser:
    """Accumulates an SSE chat stream (OpenAI format, DeepSeek reasoning, split tool calls)."""

    def __init__(self) -> None:
        self._text: list[str] = []
        self._reasoning: list[str] = []
        self._calls: dict[int, dict[str, str]] = {}
        self._finish: str | None = None
        self._usage: Usage | None = None

    def feed_line(self, line: str) -> list[TextDelta | ReasoningDelta]:
        line = line.strip()
        if not line.startswith("data:"):
            return []
        payload = line[5:].strip()
        if payload == "[DONE]":
            return []
        try:
            obj = json.loads(payload)
        except ValueError:
            return []
        if not isinstance(obj, dict):
            return []
        if obj.get("error"):
            err = obj["error"]
            msg = err.get("message", err) if isinstance(err, dict) else err
            raise LLMError(tr('Провайдер вернул ошибку: {0}', msg), raw=str(msg))
        usage = obj.get("usage")
        if isinstance(usage, dict):
            self._usage = Usage(int(usage.get("prompt_tokens") or 0), int(usage.get("completion_tokens") or 0))
        events: list[TextDelta | ReasoningDelta] = []
        for choice in obj.get("choices") or []:
            if not isinstance(choice, dict):
                continue
            delta = choice.get("delta") or {}
            reasoning = delta.get("reasoning_content") or delta.get("reasoning")
            if isinstance(reasoning, str) and reasoning:
                self._reasoning.append(reasoning)
                events.append(ReasoningDelta(reasoning))
            content = delta.get("content")
            if isinstance(content, str) and content:
                self._text.append(content)
                events.append(TextDelta(content))
            for tc in delta.get("tool_calls") or []:
                slot = self._calls.setdefault(int(tc.get("index", len(self._calls))), {"id": "", "name": "", "args": ""})
                if tc.get("id"):
                    slot["id"] = tc["id"]
                fn = tc.get("function") or {}
                if fn.get("name") and not slot["name"]:
                    slot["name"] = fn["name"]
                args = fn.get("arguments")
                if isinstance(args, dict):
                    slot["args"] = json.dumps(args)
                elif isinstance(args, str):
                    slot["args"] += args
            if choice.get("finish_reason"):
                self._finish = choice["finish_reason"]
        return events

    def result(self) -> Completion:
        calls = [
            ToolCall(slot["id"] or f"call_{uuid.uuid4().hex[:12]}", slot["name"], slot["args"])
            for _, slot in sorted(self._calls.items())
            if slot["name"]
        ]
        return Completion("".join(self._text), "".join(self._reasoning), calls, self._finish, self._usage)


def friendly_error(status: int, body: str, base_url: str) -> str:
    detail = body.strip()
    try:
        data = json.loads(body)
        err = data.get("error", data) if isinstance(data, dict) else data
        detail = err.get("message", str(err)) if isinstance(err, dict) else str(err)
    except ValueError:
        pass
    detail = detail[:300]
    hints = {
        400: tr("Запрос отклонён (400). Возможно, модель не поддерживает инструменты."),
        401: tr("API-ключ не подходит (401). Поменяйте его: /connect"),
        402: tr("На балансе провайдера закончились деньги (402)."),
        403: tr("Нет доступа (403). Проверьте ключ и права на модель."),
        404: tr('Модель или адрес не найдены (404). Адрес: {0}', base_url),
        429: tr("Слишком много запросов или исчерпан лимит (429). Подождите немного."),
    }
    head = hints.get(status) or (tr('Сервер провайдера ответил ошибкой {0}.', status) if status >= 500 else tr('Ошибка {0}.', status))
    return f"{head}\n{detail}" if detail else head


class LLMClient:
    """Talks to /chat/completions. Needs ``httpx``."""

    def __init__(self, base_url: str, api_key: str = "", timeout: float = 300.0) -> None:
        import httpx

        self._httpx = httpx
        self.base_url = base_url.rstrip("/") + "/"
        headers = {"Authorization": f"Bearer {api_key}"} if api_key else {}
        headers["User-Agent"] = "anixcode"
        self._http = httpx.AsyncClient(
            base_url=self.base_url, headers=headers,
            timeout=httpx.Timeout(connect=15.0, read=timeout, write=60.0, pool=15.0),
        )

    async def aclose(self) -> None:
        await self._http.aclose()

    async def list_models(self) -> list[str]:
        try:
            r = await self._http.get("models")
        except self._httpx.HTTPError as e:
            raise LLMError(tr('Не удаётся подключиться к {0}: {1}', self.base_url, e)) from e
        if r.status_code >= 400:
            raise LLMError(friendly_error(r.status_code, r.text, self.base_url))
        try:
            data = r.json().get("data", [])
        except (ValueError, AttributeError):
            return []
        return sorted({m["id"] for m in data if isinstance(m, dict) and m.get("id")})

    async def stream_chat(self, model: str, messages: list[dict[str, Any]],
                          tools: list[dict[str, Any]] | None = None
                          ) -> AsyncIterator[TextDelta | ReasoningDelta | Retrying | Completion]:
        """Stream one completion. Retries (with backoff) on rate limits, server errors and network
        failures, as long as nothing has been streamed yet."""
        import asyncio

        body: dict[str, Any] = {"model": model, "messages": messages, "stream": True,
                                "stream_options": {"include_usage": True}}
        if tools:
            body["tools"] = tools
        attempt = 0
        while True:
            parser = ChatStreamParser()
            started = False
            retry: tuple[float, str] | None = None
            try:
                async with self._http.stream("POST", "chat/completions", json=body) as r:
                    if r.status_code >= 400:
                        text = (await r.aread()).decode("utf-8", errors="replace")
                        if r.status_code == 400 and "stream_options" in text and "stream_options" in body:
                            body.pop("stream_options")  # some servers don't know this option
                            continue
                        if r.status_code in RETRY_STATUSES and attempt < MAX_ATTEMPTS - 1:
                            retry = (retry_delay(attempt, r.headers.get("retry-after")),
                                     tr("лимит запросов") if r.status_code == 429 else tr('ошибка сервера {0}', r.status_code))
                        else:
                            raise LLMError(friendly_error(r.status_code, text, self.base_url), r.status_code, text)
                    else:
                        async for line in r.aiter_lines():
                            for event in parser.feed_line(line):
                                started = True
                                yield event
            except self._httpx.HTTPError as e:
                timeout = isinstance(e, self._httpx.TimeoutException)
                if started:
                    raise LLMError(tr("Соединение оборвалось посреди ответа. Напишите «продолжай».")) from e
                if attempt >= MAX_ATTEMPTS - 1:
                    raise LLMError(tr("Провайдер слишком долго не отвечает (таймаут).") if timeout
                                   else tr('Не удаётся подключиться к {0}: {1}', self.base_url, e)) from e
                retry = (retry_delay(attempt), tr("таймаут") if timeout else tr("нет соединения"))
            if retry is not None:
                delay, reason = retry
                attempt += 1
                yield Retrying(attempt, MAX_ATTEMPTS - 1, delay, reason)
                await asyncio.sleep(delay)
                continue
            yield parser.result()
            return
