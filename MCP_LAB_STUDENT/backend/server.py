#!/usr/bin/env python3
"""server.py — MCP-сервер браузера: JSON-RPC 2.0 поверх stdio.

Один процесс = одна сессия Playwright (system Chrome). Каждая строка stdin —
одно JSON-RPC сообщение, каждая строка stdout — ответ. Реализовано:

    initialize / notifications/* / ping / tools/list / tools/call

Инструменты — минимальный набор для управления РЕАЛЬНЫМИ страницами
(аналог Microsoft Playwright MCP, но на Python и читаемый студентами):

    browser_navigate(url)            перейти по абсолютному http(s) URL
    browser_snapshot()               компактный снапшот: элементы с eN-refs
    browser_click(ref)               клик по элементу из снапшота
    browser_type(ref, text, submit)  ввод текста в поле (+ Enter)
    browser_press(key)               клавиша (Enter, Tab, Escape, ...)
    browser_scroll(direction)        прокрутка up/down
    browser_back()                   назад по истории

Модель НЕ получает скриншоты и сырой HTML — только структурированный
снапшот. Это и есть главная идея демо: маленькой LLM достаточно текста.

Запуск вручную:  python server.py   (печатайте JSON-RPC построчно)
Переменные:      HEADED=1 — видимый браузер;
                 WEB_START_URL — стартовая страница;
                 CDP_ENDPOINT — прицепиться к живому Chrome по CDP
                 (задаётся через --cdp у run.py/serve.py; Chrome с
                 remote-debugging поднимется сам, профиль CDP_PROFILE
                 или /tmp/agent-chrome)
"""
from __future__ import annotations

import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
import threading
import time
import urllib.parse
import urllib.request

PROTOCOL_VERSION = "2025-06-18"
_MAX_ELEMENTS = 60

# Компактный accessibility-style снапшот: только интерактивные элементы,
# стабильные eN-refs, обрезка по _MAX_ELEMENTS — страница должна помещаться
# в контекст маленькой модели. data-agent-ref ставится прямо в DOM, чтобы
# click/type могли найти элемент по ref одним селектором.
# Приоритизация снапшота — на реальных страницах сотни chrome-ссылок
# (меню, сайдбар, футер) забивают лимит. Три уровня, внутри — DOM-порядок:
#   0: элементы управления (input/select/textarea/button, searchbox…) —
#      их мало, но именно через них агент действует (поле поиска шапки
#      не должно вытесняться контентными ссылками);
#   1: элементы основного контента (main/article/списки результатов);
#   2: остальное — меню, сайдбары, футер.
_SNAPSHOT_JS = """(max) => {
  const MAIN = 'main, [role="main"], article, #mw-content-text, ' +
               '.mw-parser-output, ol, [data-testid="result"], ' +
               '[data-testid="mainline"], [hreflang]';
  const isCtl = e => e.matches('input, select, textarea, button, ' +
      '[role="searchbox"], [role="textbox"], [role="combobox"]');
  const els = [...document.querySelectorAll(
    'a[href], button, input, select, textarea, summary, ' +
    '[role="button"], [role="link"], [role="searchbox"], ' +
    '[role="textbox"], [role="combobox"], [role="menuitem"], [role="tab"], ' +
    // строки/ячейки списков: Google Drive файлы живут в row/gridcell —
    // без них модель список файлов не видит вовсе (найдено на живом
    // прогоне: 1.7b «тупила» и кликала навигацию — ей просто нечего
    // было читать)
    '[role="row"], [role="rowheader"], [role="gridcell"], ' +
    '[role="listitem"], [role="option"], [role="treeitem"]'
  )].filter(e => {
    // «Видим для клика», а не просто «есть в layout»: страницы прячут
    // элементы через visibility:hidden и коллапсирующие меню — у них
    // есть offsetParent и rects, фильтр по layout их пропускает, но
    // actionability-клик падает в timeout. Такие ref'ы — ловушка:
    // модель выбирает то, что кликнуть нельзя (живой случай: ULS-
    // кнопки языковых настроек Wikipedia).
    if (e.checkVisibility)
      return e.checkVisibility({checkVisibilityCSS: true});
    return e.offsetParent !== null || e.getClientRects().length > 0;
  });
  const rank = e => isCtl(e) ? 0 : (e.closest(MAIN) ? 1 : 2);
  els.sort((a, b) => rank(a) - rank(b));   // стабильно: Array.sort в JS
  // У <input> текста нет: accessible name живёт в связанном <label>
  // (for=/обёртка → e.labels) или aria-labelledby. Без него radio-
  // настройки Wikipedia видны модели как «input 0 type=radio = 0» —
  // без какой-либо семантики («Большой шрифт»? «Во всю ширину»?).
  const labelText = e => {
    const byAria = (e.getAttribute('aria-labelledby') || '')
      .split(/\\s+/).map(id => document.getElementById(id))
      .filter(Boolean).map(x => x.textContent).join(' ');
    const lab = e.labels && e.labels.length ? e.labels[0]
                                          : e.closest('label');
    return (byAria || (lab ? lab.textContent : '') || '')
      .trim().replace(/\\s+/g, ' ');
  };
  // Лимит имени — компактность снапшота для маленькой модели. Режем по
  // границе слова и ставим «…»: обрыв посередине слова модель читает
  // как ПОЛНЫЙ текст и ошибается («чаще все» вместо «чаще всего»).
  const cut = s => {
    s = s.trim().replace(/\\s+/g, ' ');
    if (s.length <= 60) return s;
    const t = s.slice(0, 60);
    return t.slice(0, t.lastIndexOf(' ') + 1) + '…';
  };
  return els.slice(0, max).map((e, i) => {
    e.dataset.agentRef = `e${i}`;
    const o = {ref: `e${i}`,
               role: e.getAttribute('role') || e.tagName.toLowerCase(),
               name: cut(e.getAttribute('aria-label') ||
                         e.getAttribute('title') || e.placeholder ||
                         e.textContent || labelText(e) ||
                         e.value || e.name || '')};
    if (e.tagName === 'INPUT') {
      o.input_type = e.type;
      // name-атрибут = имя радио-группы (font-size, limited-width-mode);
      // checked — какой вариант сейчас активен.
      if (e.name) o.group = e.name;
      if (e.type === 'radio' || e.type === 'checkbox') o.checked = e.checked;
    }
    // value виден и модели (что уже введено), и скриптовым политикам —
    // они обязаны быть чистыми функциями состояния.
    if ('value' in e && e.value) o.value = String(e.value).slice(0, 60);
    if (e.tagName === 'A')     o.href = (e.getAttribute('href') || '').slice(0, 80);
    // data-testid — семантический маркер на современных сайтах
    // (DuckDuckGo помечает ссылки результатов как result-title-a).
    if (e.dataset.testid) o.testid = e.dataset.testid;
    if (e.disabled || e.getAttribute('aria-disabled') === 'true') o.disabled = true;
    return o;
  });
}"""

# Основной текст страницы: контентные области в приоритете, body — fallback.
_MAIN_TEXT_JS = """(limit) => {
  const pick = s => { const e = document.querySelector(s);
    return e ? e.innerText : ''; };
  const t = pick('#mw-content-text') || pick('main') ||
            pick('[role="main"]') || pick('article') ||
            (document.body ? document.body.innerText : '');
  return t.slice(0, limit);
}"""

# Склейки без пробела: соседние inline-элементы («ПочтаКартинки» — две
# ссылки без текстового пробела в DOM, визуальный разделитель — CSS).
# innerText их не разделяет. Эвристика: кириллическая строчная →
# заглавная почти всегда граница двух «слов». Латиницу не трогаем —
# iPhone, eBay и т.п. давали бы ложные разрывы.
_GLUE_RE = re.compile(r"(?<=[а-яё])(?=[А-ЯЁ])")


def _unglue(text: str) -> str:
    return _GLUE_RE.sub(" ", text) if text else text


# Лид-абзац: первый параграф с реальным текстом (не хатноут/не пустой).
_LEAD_JS = """(limit) => {
  for (const p of document.querySelectorAll(
      '#mw-content-text p, .mw-parser-output p, article p, main p, p')) {
    const t = p.innerText.trim();
    if (t.length > 40 && !/redirects here|may refer to|^for .*see/i.test(t))
      return t.slice(0, limit);
  }
  return '';
}"""

# Скрытый текст: то, что человек НЕ видит (display:none, visibility,
# opacity:0, нулевой font-size, белое-по-белому), плюс HTML-комментарии.
# innerText такой контент пропускает — а для агента это главный канал
# indirect prompt injection. Поле hidden_text идёт в промпт отдельной
# помеченной секцией (render_state) и в detect_injection — так студент
# видит и саму атаку, и её обнаружение.
_HIDDEN_JS = """(limit) => {
  const parts = [];
  const rgb = c => (c.match(/\\d+/g) || []).map(Number);
  for (const e of document.querySelectorAll('body *')) {
    const t = (e.textContent || '').trim();
    if (!t || e.children.length > 0) continue;      // только листья
    const s = getComputedStyle(e);
    const invisible = s.display === 'none' || s.visibility === 'hidden' ||
                      parseFloat(s.opacity) === 0 ||
                      parseFloat(s.fontSize) < 2;
    // белое-по-белому: цвет текста совпадает с фоном (простая
    // проверка — точность до ближайшего предка с фоном не нужна).
    const fg = rgb(s.color), bg = rgb(s.backgroundColor);
    const camo = bg.length === 4 && bg[3] > 0 &&
                 fg.every((v, i) => Math.abs(v - bg[i]) < 25);
    if (invisible || camo) parts.push(t.slice(0, 300));
    if (parts.join(' ').length >= limit) break;
  }
  // HTML-комментарии — классический канал для «AI NOTE: ...».
  const walker = document.createTreeWalker(
    document.documentElement, NodeFilter.SHOW_COMMENT);
  let n;
  while ((n = walker.nextNode()) && parts.join(' ').length < limit)
    parts.push('COMMENT: ' + n.nodeValue.trim().slice(0, 300));
  return parts.join(' ').slice(0, limit);
}"""

TOOLS = [
    {
        "name": "browser_navigate",
        "annotations": {"readOnlyHint": True, "openWorldHint": True},
        "description": "Navigate the browser to an absolute http(s) URL.",
        "inputSchema": {
            "type": "object",
            "properties": {"url": {"type": "string"}},
            "required": ["url"],
        },
    },
    {
        "name": "browser_snapshot",
        "annotations": {"readOnlyHint": True},
        "description": "Read the page: interactive elements with stable "
                       "refs (e0, e1, ...), page URL/title, main text.",
        "inputSchema": {"type": "object", "properties": {}, "required": []},
    },
    {
        "name": "browser_click",
        "annotations": {"readOnlyHint": False, "destructiveHint": True},
        "description": "Click an element by its ref from the snapshot.",
        "inputSchema": {
            "type": "object",
            "properties": {"ref": {"type": "string"}},
            "required": ["ref"],
        },
    },
    {
        "name": "browser_type",
        "annotations": {"readOnlyHint": False, "destructiveHint": True},
        "description": "Fill an input/textarea by ref with text. "
                       "submit=true also presses Enter.",
        "inputSchema": {
            "type": "object",
            "properties": {
                "ref": {"type": "string"},
                "text": {"type": "string"},
                "submit": {"type": "boolean"},
            },
            "required": ["ref", "text"],
        },
    },
    {
        "name": "browser_press",
        "annotations": {"readOnlyHint": False},
        "description": "Press a keyboard key (Enter, Tab, Escape, arrows).",
        "inputSchema": {
            "type": "object",
            "properties": {"key": {"type": "string"}},
            "required": ["key"],
        },
    },
    {
        "name": "browser_scroll",
        "annotations": {"readOnlyHint": True},
        "description": "Scroll the page up or down.",
        "inputSchema": {
            "type": "object",
            "properties": {"direction": {"enum": ["up", "down"]}},
            "required": ["direction"],
        },
    },
    {
        "name": "browser_get_text",
        "description": "Return the visible innerText of element by ref "
                       "from the snapshot.",
        "annotations": {"readOnlyHint": True},
        "inputSchema": {
            "type": "object",
            "properties": {"ref": {"type": "string"}},
            "required": ["ref"],
        },
    },
    # BEGIN TODO student_tool_descriptor
    # === ЭТАП C: дескриптор вашего инструмента добавляется в этот список ==
    # ЧТО СДЕЛАТЬ: добавьте dict по образцу выше — name, description
    #   (его читает МОДЕЛЬ: пишите, что инструмент делает и когда нужен),
    #   inputSchema (JSON Schema: properties + required).
    # ПРИМЕР: {"name": "browser_get_text", "description": "Return the
    #   visible text of element by ref", "inputSchema": {"type": "object",
    #   "properties": {"ref": {"type": "string"}}, "required": ["ref"]}}
    # ЗАЧЕМ: tools/list отдаёт ровно этот список — модель узнаёт о
    #   возможностях агента ТОЛЬКО через дескрипторы. Нет записи здесь —
    #   tools/call ответит "unknown tool". Полный образец: README.md,
    #   этап 1 «Каталог инструментов».
    # ====================================================================
    # END TODO
    {
        "name": "browser_back",
        "annotations": {"readOnlyHint": True},
        "description": "Go back to the previous page.",
        "inputSchema": {"type": "object", "properties": {}, "required": []},
    },
    {
        "name": "browser_new_tab",
        "annotations": {"readOnlyHint": False, "openWorldHint": True},
        "description": "Open a new browser tab and switch to it; "
                       "optionally navigate to url.",
        "inputSchema": {
            "type": "object",
            "properties": {"url": {"type": "string"}},
            "required": [],
        },
    },
    {
        "name": "browser_switch_tab",
        "annotations": {"readOnlyHint": True},
        "description": "Switch to tab by index (see browser_list_tabs).",
        "inputSchema": {
            "type": "object",
            "properties": {"index": {"type": "integer"}},
            "required": ["index"],
        },
    },
    {
        "name": "browser_list_tabs",
        "annotations": {"readOnlyHint": True},
        "description": "List open browser tabs (index, title, url).",
        "inputSchema": {"type": "object", "properties": {}, "required": []},
    },
]


# ---------------------------------------------------------------------------
# CDP: если CDP_ENDPOINT задан, но по нему никто не слушает — Chrome с
# remote-debugging поднимается сам на отдельном профиле (Chrome 136+
# не пускает к основному профилю, поэтому CDP_PROFILE отдельный).
# ---------------------------------------------------------------------------
# Дефолт профиля — платформенный: на Windows /tmp не существует как
# системный каталог, берём %LOCALAPPDATA% (fallback — temp).
def _default_cdp_profile() -> str:
    if sys.platform == "win32":
        base = os.environ.get("LOCALAPPDATA") or tempfile.gettempdir()
        return os.path.join(base, "agent-chrome")
    # /tmp — tmpfs и стирается при перезагрузке, унося логины (Google
    # и т.п.): профиль кладём в XDG-cache в домашнем каталоге —
    # переживает ребут, но всё ещё «кэш», а не основной профиль
    # (2026-11-04: потерян вход после очистки /tmp).
    base = os.environ.get("XDG_CACHE_HOME") or os.path.expanduser("~/.cache")
    return os.path.join(base, "agent-chrome")


CDP_PROFILE = os.environ.get("CDP_PROFILE", _default_cdp_profile())


def _find_chrome() -> str | None:
    """Исполняемый Chrome/Chromium: PATH (linux-имена + chrome),
    плюс стандартные пути установки на Windows — там бинарь
    называется chrome.exe и в PATH обычно не прописан."""
    for name in ("google-chrome", "chromium", "google-chrome-stable",
                 "chrome"):
        path = shutil.which(name)
        if path:
            return path
    if sys.platform == "win32":
        for env in ("PROGRAMFILES", "PROGRAMFILES(X86)", "LOCALAPPDATA"):
            root = os.environ.get(env)
            if not root:
                continue
            exe = os.path.join(root, "Google", "Chrome",
                               "Application", "chrome.exe")
            if os.path.exists(exe):
                return exe
    return None


def _cdp_ready(endpoint: str, timeout: float = 1.0) -> bool:
    try:
        urllib.request.urlopen(endpoint.rstrip("/") + "/json/version",
                               timeout=timeout)
        return True
    except Exception:                                   # noqa: BLE001
        return False


def ensure_cdp_chrome(endpoint: str) -> None:
    """Автозапуск Chrome для CDP-режима: уже живой endpoint не трогаем,
    мёртвый — поднимаем chrome с --remote-debugging-port и ждём,
    пока /json/version не ответит."""
    if _cdp_ready(endpoint):
        return
    port = urllib.parse.urlparse(endpoint).port or 9222
    chrome = _find_chrome()
    if not chrome:
        raise RuntimeError(
            "CDP_ENDPOINT задан, но Chrome/Chromium не найден "
            "(ни в PATH, ни в стандартных путях установки)")
    print(f"[cdp] {endpoint} не отвечает — запускаю Chrome "
          f"(профиль {CDP_PROFILE})", file=sys.stderr, flush=True)
    subprocess.Popen(
        [chrome, f"--remote-debugging-port={port}",
         f"--user-data-dir={CDP_PROFILE}", "--no-first-run"],
        stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    for _ in range(150):
        if _cdp_ready(endpoint, timeout=0.2):
            return
        time.sleep(0.1)
    raise RuntimeError(f"Chrome не поднял CDP на {endpoint} за 15 с")


# ---------------------------------------------------------------------------
# Browser session: создаётся лениво при первом вызове инструмента и живёт
# до конца процесса. Все вызовы блокирующие — sync Playwright в stdio-цикле.
# ---------------------------------------------------------------------------
class Browser:
    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._pw = None
        self._browser = None
        self._page = None

    @property
    def page(self):
        if self._page is None or self._page.is_closed():
            from playwright.sync_api import sync_playwright
            self._pw = sync_playwright().start()
            cdp = os.environ.get("CDP_ENDPOINT", "")
            if cdp:
                # CDP-режим: Chrome с remote-debugging либо уже запущен
                # пользователем, либо поднимется тут сам (ensure_cdp_-
                # chrome). Видим вкладки и сессии этого профиля
                # (логины, куки). Отключение НЕ закрывает браузер.
                ensure_cdp_chrome(cdp)
                self._browser = self._pw.chromium.connect_over_cdp(cdp)
                ctx = self._browser.contexts[0]
                # Активная вкладка — последняя http(s)-страница в
                # контексте; ctx.pages[0] может попасть на iframe/
                # browser-UI таргет или пустую вкладку.
                pages = [p for p in ctx.pages
                         if p.url.startswith("http")]
                self._page = (pages[-1] if pages
                              else (ctx.pages[0] if ctx.pages
                                    else ctx.new_page()))
            else:
                headless = os.environ.get("HEADED", "0") != "1"
                # channel="chrome" — системный Chrome, `playwright
                # install` студентам не нужен.
                self._browser = self._pw.chromium.launch(
                    channel="chrome", headless=headless)
                self._page = self._browser.new_page()
            start = os.environ.get("WEB_START_URL")
            if start:
                self._page.goto(start, wait_until="domcontentloaded")
        return self._page

    def snapshot(self) -> dict:
        p = self.page
        els = p.evaluate(_SNAPSHOT_JS, _MAX_ELEMENTS)
        # ref → {role,name}: SPA вроде Drive перерисовывает DOM между
        # снапшотом и кликом — data-agent-ref теряется, имя остаётся.
        self._refs = {e["ref"]: e for e in els}
        return {
            "url": p.url,
            "title": p.title(),
            "elements": els,
            "main_text": _unglue(p.evaluate(_MAIN_TEXT_JS, 1500)),
            "lead_text": _unglue(p.evaluate(_LEAD_JS, 400)),
            "hidden_text": p.evaluate(_HIDDEN_JS, 800),
        }

    def call(self, name: str, args: dict) -> str:
        """Выполнить browser_* инструмент; вернуть текст результата."""
        with self._lock:
            p = self.page
            if name == "browser_navigate":
                p.goto(args["url"], wait_until="domcontentloaded")
                p.wait_for_timeout(500)
                return f"navigated to {p.url}"
            if name == "browser_snapshot":
                return json.dumps(self.snapshot(), ensure_ascii=False)
            if name == "browser_click":
                sel = f'[data-agent-ref="{args["ref"]}"]'
                loc = p.locator(sel)
                try:
                    # Элемент невидим (выпадающее меню Wikipedia закрылось
                    # между снапшотом и кликом): actionability-ожидание
                    # сожжёт весь таймаут и всё равно упадёт — сразу идём
                    # в fallback, не платя 3 с за заведомый провал.
                    if not loc.count() or not loc.first.is_visible():
                        raise TimeoutError("element detached or hidden")
                    p.click(sel, timeout=3000)
                except Exception:
                    # Ссылка устарела (SPA перерисовала DOM) или элемент
                    # скрылся: ищем заново по роли и имени из снапшота.
                    info = getattr(self, "_refs", {}).get(
                        args["ref"]) or {}
                    role = info.get("role", "")
                    nm = info.get("name", "")
                    frag = nm[:30]
                    if not nm:
                        # Имя неизвестно, но элемент на месте — не кликабелен
                        # для actionability, а DOM-событие сработает.
                        if loc.count():
                            loc.first.dispatch_event("click")
                            p.wait_for_timeout(500)
                            return f"clicked {args['ref']}"
                        raise RuntimeError(
                            f"element {args['ref']} not clickable — скрыт "
                            "в свёрнутом меню: сначала кликните триггер "
                            "меню или используйте другой путь") from None
                    # role в снапшоте — значение атрибута или тег;
                    # ищем кандидата с тем же role/name-фрагментом.
                    el = p.evaluate(
                        """([role, frag]) => {
                          const sel = role.match(/^[a-z]+$/)
                            ? `[role="${role}"], ${role}` : '*';
                          for (const e of document.querySelectorAll(sel)) {
                            const n = (e.getAttribute('aria-label') ||
                                       e.textContent || '').trim();
                            if (n.includes(frag.slice(0, 25)))
                              return e.setAttribute('data-agent-ref',
                                                    '__fix'), 1;
                          }
                          return 0;
                        }""", [role, frag])
                    if not el:
                        # По имени не перенайден (имя в title/aria-label,
                        # а не в innerText), но элемент есть — последний
                        # шанс: DOM-событие прямо на нём.
                        if loc.count():
                            loc.first.dispatch_event("click")
                        else:
                            raise RuntimeError(
                                f"element {args['ref']} not clickable — "
                                "скрыт в свёрнутом меню: сначала кликните "
                                "триггер меню или используйте другой путь") from None
                    else:
                        fix = p.locator('[data-agent-ref="__fix"]').first
                        if fix.is_visible():
                            p.click('[data-agent-ref="__fix"]', timeout=3000)
                        else:
                            # Нашли, но элемент невидим — настоящий клик не
                            # пройдёт никогда. DOM-событие click всё равно
                            # вызывает activation behavior: radio-настройки
                            # переключаются, обработчики пунктов меню
                            # срабатывают. Без этого агент буквально не может
                            # выбрать опцию из закрывшегося меню.
                            fix.dispatch_event("click")
                p.wait_for_timeout(500)
                return f"clicked {args['ref']}"
            if name == "browser_type":
                sel = f'[data-agent-ref="{args["ref"]}"]'
                # fill() работает только с настоящими input/textarea;
                # contenteditable-элементы (Google Sheets/Docs, rich-
                # редакторы) заполняем через фокус + посимвольный ввод.
                kind = p.eval_on_selector(
                    sel, "el => el.tagName + '|' + String("
                         "el.isContentEditable)")
                tag, editable = kind.split("|", 1)
                if tag in ("INPUT", "TEXTAREA"):
                    p.fill(sel, args["text"], timeout=5000)
                elif editable == "true":
                    p.click(sel, timeout=5000)
                    p.keyboard.type(args["text"], delay=10)
                else:
                    raise ValueError(
                        f"element {args['ref']} is not typeable "
                        f"({tag.lower()})")
                if args.get("submit"):
                    p.press(sel, "Enter")
                    p.wait_for_timeout(800)
                return f"typed into {args['ref']}"
            if name == "browser_press":
                # Модели шлют "DELETE", "RETURN", "CONTROL+A" —
                # нормализуем в playwright-имена (Delete, Enter, ...).
                _KEYS = {"delete": "Delete", "del": "Delete",
                         "return": "Enter", "enter": "Enter",
                         "esc": "Escape", "escape": "Escape",
                         "backspace": "Backspace", "space": " ",
                         "tab": "Tab", "home": "Home", "end": "End",
                         "pageup": "PageUp", "pagedown": "PageDown",
                         "arrowup": "ArrowUp", "arrowdown": "ArrowDown",
                         "arrowleft": "ArrowLeft", "arrowright": "ArrowRight"}
                key = args["key"]
                if "+" in key:
                    key = "+".join(_KEYS.get(k.strip().lower(), k.strip())
                                   for k in key.split("+"))
                else:
                    key = _KEYS.get(key.lower(), key[:1].upper() + key[1:])
                p.keyboard.press(key)
                p.wait_for_timeout(300)
                return f"pressed {args['key']}"
            if name == "browser_scroll":
                p.mouse.wheel(0, 700 if args["direction"] == "down" else -700)
                p.wait_for_timeout(300)
                return f"scrolled {args['direction']}"
            if name == "browser_back":
                p.go_back(wait_until="domcontentloaded")
                return f"back to {p.url}"
            if name == "browser_get_text":
                return p.inner_text(
                    f'[data-agent-ref="{args["ref"]}"]')[:2000]
            if name == "browser_new_tab":
                ctx = self._page.context
                self._page = ctx.new_page()
                self._page.bring_to_front()
                if args.get("url"):
                    self._page.goto(args["url"],
                                    wait_until="domcontentloaded")
                    self._page.wait_for_timeout(500)
                return f"new tab: {self._page.url}"
            if name == "browser_list_tabs":
                tabs = [{"index": i, "title": pg.title(), "url": pg.url}
                        for i, pg in enumerate(self._page.context.pages)]
                return json.dumps(tabs, ensure_ascii=False)
            if name == "browser_switch_tab":
                pages = self._page.context.pages
                idx = int(args["index"])
                if not (0 <= idx < len(pages)):
                    return f"no tab {idx}: {len(pages)} open"
                self._page = pages[idx]
                self._page.bring_to_front()
                return f"switched to tab {idx}: {self._page.url}"
            # BEGIN TODO student_tool_dispatch
            # === ЭТАП C: здесь добавляется ветка dispatch для вашего =====
            #     инструмента:
            # ЧТО СДЕЛАТЬ: добавьте `if name == "browser_get_text":` ДО
            #   строки raise ниже. В ветке выполните действие над страницей
            #   `p` (объект Playwright Page) и верните str — результат
            #   уйдёт модели как текст вызова.
            # ПРИМЕР:  return p.inner_text(
            #              f'[data-agent-ref="{args["ref"]}"]')[:2000]
            #   (элементы в снапшоте помечены атрибутом data-agent-ref —
            #   так ref из снапшота находит реальный DOM-элемент).
            # ЗАЧЕМ: сервер — «глупый» исполнитель: вся логика проверок на
            #   хосте, здесь только механика действия + строка-результат.
            #   Ограничьте вывод (~2000 символов) — он целиком попадёт
            #   в контекст модели.
            # ============================================================
            # END TODO
            raise ValueError(f"unknown tool: {name}")


def respond(req_id, result=None, error=None):
    msg = {"jsonrpc": "2.0", "id": req_id}
    if error:
        msg["error"] = error
    else:
        msg["result"] = result
    print(json.dumps(msg, ensure_ascii=False), flush=True)


def main() -> None:
    # На Windows stdout subprocess'а — cp1251: символы вроде «←» в
    # снапшотах страниц падали бы charmap-ошибкой внутри respond().
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    browser = Browser()
    known = {t["name"] for t in TOOLS}
    for line in sys.stdin:
        line = line.strip()
        if not line:
            continue
        try:
            req = json.loads(line)
        except json.JSONDecodeError:
            respond(None, error={"code": -32700, "message": "Parse error"})
            continue

        method, req_id = req.get("method"), req.get("id")
        if req_id is None:            # notification — ответ не нужен
            continue
        if not isinstance(method, str):
            respond(req_id, error={"code": -32600,
                                   "message": "Invalid Request"})
            continue

        if method == "initialize":
            respond(req_id, result={
                "protocolVersion": PROTOCOL_VERSION,
                "capabilities": {"tools": {"listChanged": False}},
                "serverInfo": {"name": "browser-agent", "version": "0.1.0"},
            })
        elif method == "ping":
            respond(req_id, result={})
        elif method == "tools/list":
            respond(req_id, result={"tools": TOOLS})
        elif method == "tools/call":
            params = req.get("params") or {}
            name, args = params.get("name"), params.get("arguments") or {}
            # === ЭТАП C: добавьте имя вашего инструмента в TOOLS выше,
            #     иначе вызов отклонится как unknown tool ===
            if name not in known:
                respond(req_id, error={"code": -32602,
                                       "message": f"unknown tool: {name}"})
                continue
            try:
                text = browser.call(name, args)
                respond(req_id, result={
                    "content": [{"type": "text", "text": text}],
                    "isError": False,
                })
            except Exception as e:                    # noqa: BLE001
                # Ошибка страницы (таймаут, элемент исчез) возвращается
                # модели как isError — агент должен уметь восстанавливаться.
                respond(req_id, result={
                    "content": [{"type": "text", "text": f"ERROR: {e}"}],
                    "isError": True,
                })
        else:
            respond(req_id, error={"code": -32601,
                                   "message": f"Method not found: {method}"})


if __name__ == "__main__":
    main()
