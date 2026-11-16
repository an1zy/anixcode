"""anixcode [folder] | anixcode run "task" | anixcode connect | anixcode models"""
from __future__ import annotations
from .i18n import tr

import argparse
import asyncio
import getpass
import sys
from pathlib import Path

from . import __version__, providers
from .agent import Agent, AgentError, Compacted, PermissionRequest, ToolFinished, ToolStarted
from .airpc import AirpcReporter
from .llm import LLMClient, LLMError, ReasoningDelta, Retrying, TextDelta
from .settings import Credentials, config_dir, load_auth, load_settings, save_auth, save_settings
from .storage import Store

COMMANDS = {"run", "connect", "models"}


def _client(settings, auth) -> LLMClient:
    p = providers.get(settings.provider, settings)
    cred = auth.get(settings.provider, Credentials())
    return LLMClient(cred.base_url or p.base_url, cred.api_key)


def cmd_tui(args: argparse.Namespace) -> int:
    try:
        from .tui.app import AnixApp
    except ImportError as e:
        print(tr('Не хватает библиотеки {0}. Установите: pip install -e .', e.name), file=sys.stderr)
        return 1
    from .projects import is_unsuitable

    root = Path(args.folder or ".").resolve()
    if not root.is_dir():
        print(tr('Нет такой папки: {0}', root), file=sys.stderr)
        return 2
    # Started from home, a drive root or C:\Windows\System32 (a fresh cmd window)? Ask which project.
    pick = args.folder is None and is_unsuitable(root)
    AnixApp(root, load_settings(), Store(config_dir() / "sessions.db"), pick_project=pick).run()
    return 0


def cmd_connect(_args: argparse.Namespace) -> int:
    settings, auth = load_settings(), load_auth()
    print(tr("Провайдеры:"))
    for i, p in enumerate(providers.PROVIDERS, 1):
        print(f"  {i}. {p.name}")
    choice = input(tr("Номер: ")).strip()
    try:
        p = providers.PROVIDERS[int(choice) - 1]
    except (ValueError, IndexError):
        print(tr("Неверный номер."))
        return 2
    cred = auth.get(p.id, Credentials())
    base = input(tr('Адрес сервера [{0}]: ', cred.base_url or p.base_url)).strip() or cred.base_url or p.base_url
    if p.key_url:
        print(tr('Ключ можно взять здесь: {0}', p.key_url))
    key = getpass.getpass(tr("API-ключ (Enter — оставить прежний): ")).strip()
    if key:
        cred.api_key = key
    cred.base_url = "" if base == p.base_url else base
    model = input(tr('Модель [{0}]: ', p.default_model or settings.model)).strip() or p.default_model or settings.model
    if not model:
        print(tr("Нужно указать модель. Список: anixcode models"))
    auth[p.id] = cred
    settings.provider, settings.model = p.id, model
    save_auth(auth)
    save_settings(settings)
    print(tr('Сохранено: {0} · {1}', p.name, model))
    return 0


def cmd_models(_args: argparse.Namespace) -> int:
    settings, auth = load_settings(), load_auth()
    if not settings.provider:
        print(tr("Сначала: anixcode connect"), file=sys.stderr)
        return 2

    async def go() -> list[str]:
        client = _client(settings, auth)
        try:
            return await client.list_models()
        finally:
            await client.aclose()

    try:
        for m in asyncio.run(go()):
            print(m)
    except LLMError as e:
        print(e, file=sys.stderr)
        return 1
    return 0


def cmd_run(args: argparse.Namespace) -> int:
    settings, auth = load_settings(), load_auth()
    if not settings.ready:
        print(tr("Сначала подключите модель: anixcode connect"), file=sys.stderr)
        return 2
    root = Path(args.folder or ".").resolve()
    prompt = args.prompt
    if prompt.startswith("/"):  # ready-made and own prompt commands work here too: anixcode run "/review"
        from . import commands

        name, rest = commands.parse(prompt)
        cmd = commands.find(name, commands.all_commands(root, config_dir()))
        if cmd is None or cmd.template is None:
            print(tr('Команда /{0} работает только в интерфейсе или не существует.', name), file=sys.stderr)
            return 2
        prompt = commands.render(cmd.template, rest)

    async def ask(req: PermissionRequest) -> str:
        for why in req.danger:
            print(f"\n\033[91m⚠ {why}\033[0m", file=sys.stderr)
        for why in req.warnings:
            print(f"\033[93m! {why}\033[0m", file=sys.stderr)
        if args.yes:
            return "once"
        print(f"\n? {req.title}", file=sys.stderr)
        if req.preview:
            print(req.preview, file=sys.stderr)
        answer = (await asyncio.to_thread(input, tr("Разрешить? [y]es / [a]lways / [n]o: "))).strip().lower()
        return {"y": "once", "a": "always"}.get(answer[:1], "deny")

    async def go() -> int:
        client = _client(settings, auth)
        store = Store(config_dir() / "sessions.db")
        session = store.create(str(root), settings.model)
        agent = Agent(client, settings.model, root, settings, ask=ask, reporter=AirpcReporter(settings.airpc),
                      provider=settings.provider)
        agent.mode = "plan" if args.plan else "build"
        code = 0
        try:
            async for ev in agent.run(session, prompt):
                if isinstance(ev, TextDelta):
                    print(ev.text, end="", flush=True)
                elif isinstance(ev, ReasoningDelta) and args.verbose:
                    print(ev.text, end="", file=sys.stderr, flush=True)
                elif isinstance(ev, ToolStarted):
                    print(f"\n· {ev.title}", file=sys.stderr, flush=True)
                elif isinstance(ev, ToolFinished) and ev.result.error:
                    print(f"  ✗ {ev.result.output.splitlines()[0] if ev.result.output else ''}", file=sys.stderr)
                elif isinstance(ev, AgentError):
                    print(f"\n✗ {ev.message}", file=sys.stderr)
                    code = 1
                elif isinstance(ev, Retrying):
                    print(tr('\n… повтор {0}/{1} через {2:.0f}с ({3})', ev.attempt, ev.max_attempts, ev.delay, ev.reason), file=sys.stderr)
                elif isinstance(ev, Compacted):
                    print(tr('\n🗜 история сжата: ~{0} → ~{1} токенов', ev.before, ev.after), file=sys.stderr)
        finally:
            await client.aclose()
            store.close()
        print()
        return code

    try:
        return asyncio.run(go())
    except KeyboardInterrupt:
        return 130


def main(argv: list[str] | None = None) -> int:
    argv = list(sys.argv[1:] if argv is None else argv)
    if argv and argv[0] in ("-V", "--version"):
        print(f"anixcode {__version__}")
        return 0
    parser = argparse.ArgumentParser(prog="anixcode", description=tr("ИИ-агент для кода в терминале"))
    if argv and argv[0] in COMMANDS:
        sub = parser.add_subparsers(dest="command", required=True)
        p = sub.add_parser("run", help=tr("выполнить задачу без интерфейса"))
        p.add_argument("prompt")
        p.add_argument("folder", nargs="?")
        p.add_argument("--plan", action="store_true", help=tr("только читать и планировать"))
        p.add_argument("-y", "--yes", action="store_true", help=tr("разрешать все действия без вопросов"))
        p.add_argument("-v", "--verbose", action="store_true", help=tr("показывать размышления модели"))
        p.set_defaults(func=cmd_run)
        sub.add_parser("connect", help=tr("подключить модель и API-ключ")).set_defaults(func=cmd_connect)
        sub.add_parser("models", help=tr("список моделей провайдера")).set_defaults(func=cmd_models)
    else:
        parser.add_argument("folder", nargs="?", help=tr("папка проекта (по умолчанию текущая)"))
        parser.set_defaults(func=cmd_tui)
    args = parser.parse_args(argv)
    return args.func(args)
