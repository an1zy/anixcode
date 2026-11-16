"""Spotting risky shell commands so the permission dialog can warn in red.

This is a safety net, not a sandbox: it catches common destructive commands in bash, cmd and
PowerShell. The real protection is that every command needs the user's approval.
"""
from __future__ import annotations
from .i18n import tr

import re
from dataclasses import dataclass, field
from pathlib import Path


@dataclass
class Risk:
    danger: list[str] = field(default_factory=list)  # destructive: shown in red, always asks
    warnings: list[str] = field(default_factory=list)  # worth a look: shown in yellow

    @property
    def dangerous(self) -> bool:
        return bool(self.danger)


_I = re.IGNORECASE
DANGER: tuple[tuple[re.Pattern[str], str], ...] = tuple((re.compile(p, _I), why) for p, why in (
    (r"\brm\s+(-[a-z]*[rf][a-z]*\s+)+", tr("удаление файлов без возврата (rm -r / -f)")),
    (r"\brm\s+(?!-)", tr("удаление файлов (rm)")),
    (r"\b(rmdir|rd)\b.*(/s|-r)", tr("удаление папки со всем содержимым")),
    (r"\b(del|erase)\b", tr("удаление файлов (del)")),
    (r"\bremove-item\b|\bri\s", tr("удаление файлов (Remove-Item)")),
    (r"\bclear-content\b|\bclc\s", tr("очистка содержимого файлов")),
    (r"\bformat(-volume)?\s+[a-z]:|\bformat\s+/|\bmkfs\b|\bdiskpart\b|\bclear-disk\b", tr("форматирование диска")),
    (r"\bdd\s+.*\bof=", tr("запись на диск напрямую (dd)")),
    (r">\s*/dev/(sd|nvme|disk)", tr("запись на диск напрямую")),
    (r"\bgit\s+reset\s+--hard\b", tr("git reset --hard: несохранённые изменения пропадут")),
    (r"\bgit\s+clean\s+-[a-z]*f", tr("git clean: удаление неотслеживаемых файлов")),
    (r"\bgit\s+push\b.*(--force\b|\s-f\b|--force-with-lease)", tr("принудительный git push: можно затереть чужие коммиты")),
    (r"\bgit\s+(checkout|restore)\s+(--\s+)?\.(\s|$)", tr("откат всех незакоммиченных изменений")),
    (r"\bgit\s+branch\s+-D\b", tr("удаление ветки без проверки")),
    (r"\b(curl|wget|iwr|invoke-webrequest|irm|invoke-restmethod)\b.*\|\s*(sh|bash|zsh|iex|invoke-expression|python)",
     tr("скачивание и запуск скрипта из интернета")),
    (r"\biex\b|\binvoke-expression\b", tr("выполнение произвольного кода (Invoke-Expression)")),
    (r"\b(shutdown|reboot|poweroff|restart-computer|stop-computer)\b", tr("выключение или перезагрузка компьютера")),
    (r"\breg\s+(delete|add)\b|\bremove-itemproperty\b|\bset-itemproperty\b.*hk(lm|cu)", tr("изменение реестра Windows")),
    (r"\bset-executionpolicy\b", tr("изменение политики запуска скриптов")),
    (r"\bbcdedit\b|\bcipher\s+/w\b|\bvssadmin\b.*delete", tr("системная утилита, опасная для данных")),
    (r"\bchmod\s+-r\b|\bchmod\s+777\b|\bchown\s+-r\b", tr("массовая смена прав доступа")),
    (r"\bsudo\b|\brunas\b|-verb\s+runas", tr("запуск с правами администратора")),
    (r"\b(drop\s+(table|database|schema)|truncate\s+table)\b", tr("удаление данных из базы")),
    (r"\b(npm|pnpm|yarn)\s+publish\b|\btwine\s+upload\b|\bcargo\s+publish\b", tr("публикация пакета")),
    (r":\(\)\s*\{\s*:\|:&\s*\};:", tr("fork-бомба")),
    (r"\b(taskkill\b.*/f|stop-process\b|kill\s+-9|killall|pkill)\b", tr("принудительное завершение процессов")),
))
WARN: tuple[tuple[re.Pattern[str], str], ...] = tuple((re.compile(p, _I), why) for p, why in (
    (r"\b(mv|move|move-item|ren|rename-item)\b", tr("перемещение или переименование файлов")),
    (r"(^|[^>])>\s*[^&\s|]", tr("перезапись файла через >")),
    (r"\b(pip|pip3|npm|pnpm|yarn|cargo|choco|winget|apt|apt-get|brew)\s+(install|add|uninstall|remove)\b",
     tr("установка или удаление пакетов")),
    (r"\bgit\s+(push|commit\s+--amend|rebase)\b", tr("изменение истории или отправка в удалённый репозиторий")),
    (r"\b(curl|wget|iwr|invoke-webrequest)\b", tr("обращение в интернет")),
))
_OUTSIDE = re.compile(
    r"(\$env:userprofile|%userprofile%|\$home\b|~[/\\]|getfolderpath|\bdesktop\b|рабочий стол|"
    r"(?<![\w.])[a-z]:[\\/]|(?<![\w.~])/(home|users|etc|usr|var|mnt|media|opt|root)\b)", _I)


def check_command(command: str, root: Path | None = None) -> Risk:
    risk = Risk()
    text = " ".join(command.split())
    for pattern, why in DANGER:
        if pattern.search(text) and why not in risk.danger:
            risk.danger.append(why)
    # "rm -rf" also matches plain "rm": keep the more specific reason only
    if len([d for d in risk.danger if d.startswith(tr("удаление файлов"))]) > 1:
        risk.danger = [d for d in risk.danger if d != tr("удаление файлов (rm)")]
    for pattern, why in WARN:
        if pattern.search(text) and why not in risk.warnings:
            risk.warnings.append(why)
    for m in _OUTSIDE.finditer(text):
        hit = m.group(0)
        if root is not None and str(root).lower() in text.lower()[max(0, m.start() - 1): m.start() + len(str(root)) + 1]:
            continue  # an absolute path into the project itself
        risk.warnings.append(tr('затрагивает файлы вне папки проекта ({0})', hit.strip()))
        break
    return risk
