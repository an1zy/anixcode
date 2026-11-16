"""Known OpenAI-compatible providers, plus the user's own ones (stored in settings.json)."""
from __future__ import annotations
from .i18n import tr

import re
from dataclasses import dataclass
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from .settings import Settings


@dataclass(frozen=True)
class Provider:
    id: str
    name: str
    base_url: str
    needs_key: bool = True
    default_model: str = ""
    key_url: str = ""  # where to get a key

    @property
    def is_user(self) -> bool:
        return self.id.startswith("user:")


PROVIDERS: tuple[Provider, ...] = (
    Provider("deepseek", "DeepSeek", "https://api.deepseek.com/v1", default_model="deepseek-chat",
             key_url="https://platform.deepseek.com/api_keys"),
    Provider("openai", "OpenAI", "https://api.openai.com/v1", key_url="https://platform.openai.com/api-keys"),
    Provider("anthropic", "Anthropic (Claude)", "https://api.anthropic.com/v1/",
             key_url="https://console.anthropic.com/settings/keys"),
    Provider("gemini", "Google Gemini", "https://generativelanguage.googleapis.com/v1beta/openai/",
             key_url="https://aistudio.google.com/apikey"),
    Provider("groq", "Groq", "https://api.groq.com/openai/v1", key_url="https://console.groq.com/keys"),
    Provider("anix", tr("ANIX Router (локально)"), "http://localhost:3001/v1", needs_key=False),
    Provider("ollama", tr("Ollama (локально)"), "http://localhost:11434/v1", needs_key=False),
    Provider("lmstudio", tr("LM Studio (локально)"), "http://localhost:1234/v1", needs_key=False),
    Provider("custom", tr("Свой сервер (OpenAI-совместимый)"), "", needs_key=False),
)
BY_ID = {p.id: p for p in PROVIDERS}


def user_providers(settings: Settings | None) -> list[Provider]:
    if settings is None:
        return []
    return [Provider(d["id"], d["name"], d["base_url"], needs_key=False) for d in settings.custom_providers]


def all_providers(settings: Settings | None) -> list[Provider]:
    """The user's own providers first, then the built-in ones."""
    return [*user_providers(settings), *PROVIDERS]


def get(provider_id: str, settings: Settings | None = None) -> Provider:
    for p in user_providers(settings):
        if p.id == provider_id:
            return p
    return BY_ID.get(provider_id) or BY_ID["custom"]


def make_id(name: str, existing: set[str]) -> str:
    slug = re.sub(r"[^a-z0-9]+", "-", name.lower()).strip("-") or "provider"
    candidate, n = f"user:{slug}", 2
    while candidate in existing:
        candidate, n = f"user:{slug}-{n}", n + 1
    return candidate
