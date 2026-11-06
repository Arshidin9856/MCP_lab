"""scenarios.py — сценарии лабораторной: что агенту МОЖНО на сайте.

Scenario = где стартуем (url), куда агенту РАЗРЕШЕНО уходить (domains —
hard scope для browser_navigate и побочных кликов) и политика доступа:
read_only / deny_tools / out_of_scope / context_prompt /
confirm_tools / confirm_name_patterns.

Реестр SCENARIOS хранит именованные сценарии (вариант 0 — drive,
учебный minidrive); scenario_for_url() подхватывает их по домену
целевого URL, а для незнакомых доменов строит динамический «web»
в безопасном read_only.
"""
from __future__ import annotations

import re
from dataclasses import dataclass, replace
from urllib.parse import urlparse


@dataclass
class Scenario:
    name: str
    url: str
    domains: list[str]               # hard scope для browser_navigate
    example_task: str
    # --- Политика доступа — что агенту МОЖНО на этом сайте --------------
    read_only: bool = False          # только чтение: click/type/press
                                     # будут отклоняться PolicyGate
    deny_tools: tuple[str, ...] = () # имена browser_* инструментов,
                                     # запрещённых на этом сайте
    out_of_scope: tuple[str, ...] = ()  # per-site стоп-слова для команд
    context_prompt: str = ""         # приписка к задаче для LLM
                                     # (особенности сайта, подсказки)
    # --- Подтверждение человеком (human-in-the-loop) --------------------
    # Деструктивность на чужих сайтах живёт в ДАННЫХ, а не в имени
    # инструмента: вреден не browser_click вообще, а клик по кнопке
    # «Delete». Поэтому два механизма:
    confirm_tools: tuple[str, ...] = ()
    #   инструменты, которые на этом сайте ВСЕГДА требуют Approve
    confirm_name_patterns: tuple[str, ...] = ()
    #   regex по имени элемента из снапшота: клик/ввод по элементу,
    #   чьё имя матчится («delete», «удалить», «share») → сначала
    #   спросить человека. Отсутствие confirm-обработчика = отказ
    #   (fail-closed), таймаут ответа = отказ.
    # --- Отбор сценария -------------------------------------------------
    match: str = ""
    #   regex на полный URL: если задан, сценарий выбирается только при
    #   совпадении (нужен, когда несколько сценариев живут на одном
    #   домене — minidrive и attacklab оба на localhost).


# --- URL целевой страницы ----------------------------------------------------
URL_RE = re.compile(r"https?://[^\s«»\"'<>()]+")


def extract_url(task: str) -> str | None:
    """Первый http(s)-URL из текста пользователя или None."""
    m = URL_RE.search(task or "")
    return m.group(0).rstrip(".,;:!?") if m else None


# --- Реестр именованных сценариев ---------------------------------------------
# Вариант 0 лабораторной (drive) и его проверяемый офлайн-эквивалент
# (minidrive на attack_pages). Ключ — имя сценария.
SCENARIOS: dict[str, Scenario] = {}

# === ВАШ СЦЕНАРИЙ (этап 2 лабы) ======================================
# Любой сайт без записи работает в безопасном read_only («web»).
# Чтобы дать агенту ДЕЙСТВИЯ на своём сайте — зарегистрируйте
# именованный сценарий (образец — в INSTRUCTIONS.md, этап 2):
#
# SCENARIOS["my_site"] = Scenario(
#     name="my_site", url="https://example.com",
#     domains=["example.com"],
#     example_task="...",
#     read_only=False,
#     # context_prompt — подсказка от кнопки «Разведать»: навигация
#     # сайта для модели. НЕ повторяйте сюда запреты — их исполняет
#     # политика, промпт ей не замена и не усиление.
#     context_prompt="навигационная подсказка из разведки",
#     match=r"example\.com")


def scenario_for_url(url: str) -> Scenario:
    """Сценарий под целевую страницу.

    Если домен страницы покрыт именованным сценарием из SCENARIOS —
    возвращаем его копию с url=запрошенная страница (стартовый URL
    берёт пользователь, политика — из реестра).

    Незнакомый домен → динамический «web»: scope = домен ссылки,
    read_only=True намеренно — незнакомый сайт открывается в
    безопасном режиме «только чтение».

    === Политика для вашего сайта (студенческий TODO) ===============
    Чтобы дать агенту действия на своём сайте, зарегистрируйте
    сценарий в SCENARIOS выше по образцу minidrive/drive:
      read_only=False               # разрешить click/type/press —
                                    # только осознанно
      deny_tools=("browser_type",)  # инструмент под запретом всегда
      out_of_scope=("купить", "оплат")  # стоп-слова команд для сайта
      context_prompt="Search is in the header; results paginate."
      confirm_name_patterns=(r"delete", r"pay")  # спросить человека
    ЗАЧЕМ: least privilege — агенту даётся минимум возможностей для
    задачи. Стоп-слова и явный запрет инструментов дешевле и надёжнее,
    чем надежда на аккуратность модели; подтверждение человеком —
    последний рубеж перед необратимым действием.
    ================================================================"""
    host = (urlparse(url).hostname or "").lower()
    base = host[4:] if host.startswith("www.") else host
    # 1) точное совпадение по match-regex на весь URL (приоритет —
    #    порядок объявления в SCENARIOS: конкретные первыми)
    for sc in SCENARIOS.values():
        if sc.match and re.search(sc.match, url, re.IGNORECASE):
            return replace(sc, url=url)
    # 2) доменное совпадение для сценариев без match
    for sc in SCENARIOS.values():
        if not sc.match and base and any(
                base == d or base.endswith("." + d) or d.endswith("." + base)
                for d in sc.domains):
            return replace(sc, url=url)
    return Scenario(
        name="web",
        url=url,
        domains=[base] if base else [],
        example_task="",
        read_only=True)
