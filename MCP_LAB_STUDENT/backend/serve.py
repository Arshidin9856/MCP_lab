"""serve.py — API-бэкенд демо: SSE-стрим шагов агента.

    python serve.py [--port 8090]

    UI отдаёт vite dev-сервер:  cd frontend && npm run dev
                                → http://localhost:5173
    (vite проксирует /api на этот порт 8090)

    API:  GET  /api/scenarios            сценарии + примеры задач
          POST /api/run {scenario, task, headed, target, cdp}
                         → SSE поток событий
          POST /api/stop                 остановить текущий прогон
"""
from __future__ import annotations

import argparse
import http.server
import json
import os
import signal
import subprocess
import sys
import threading
import time
import traceback
import urllib.request
from pathlib import Path
from urllib.parse import urlparse

ROOT = Path(__file__).resolve().parent
sys.path.insert(0, str(ROOT))

from agent import (analyze_site, load_session, run_agent)     # noqa: E402
from llm import get_llm                                       # noqa: E402
from scenarios import extract_url, scenario_for_url           # noqa: E402

# Пауза между шагами, когда браузер видимый — чтобы глаз успевал.
HEADED_STEP_DELAY = 0.4

_env = ROOT / ".env"
if _env.exists():
    for line in _env.read_text().splitlines():
        line = line.strip()
        if line and not line.startswith("#") and "=" in line:
            k, v = line.split("=", 1)
            os.environ.setdefault(k.strip(), v.split("#", 1)[0].strip())


class ClientGone(Exception):
    """SSE-клиент отключился посреди стрима."""


# Любая ошибка записи в мёртвый сокет: BrokenPipe/ConnReset — OSError,
# «I/O operation on closed file» — ValueError при гонке с закрытием.
DEAD_SOCKET = (OSError, ValueError)

# Пока LLM думает (30–60 с), по соединению ничего не идёт — heartbeat
# удерживает его живым и сразу обнаруживает отвалившегося клиента.
# Было 15с — Firefox обрывал fetch-стрим раньше первого пинга (~9с
# тишины на живой странице), укоротили: тишины >5с теперь нет вообще.
PING_EVERY_SEC = 5

# Одновременно идёт не более одного прогона: каждый run_agent владеет
# собственным браузером, но параллельные прогоны бессмысленны для UI
# и даром жгут CPU/RAM — второй запрос отклоняем сразу.
RUN_LOCK = threading.Lock()

# Метка ЭКЗЕМПЛЯРА процесса: рестарт serve.py обнуляет EVENTS/CURR/LAST
# в памяти, а чат во фронте живёт в sessionStorage и реплеился бы
# фантомом — история лежит в sessionStorage + last_run.json. BOOT_ID
# отдаём в /api/events для отладки (UI его не сбрасывает).
BOOT_ID = f"{time.time():.3f}-{os.getpid()}"

# Кнопка «Стоп» в UI: POST /api/stop взводит событие, цикл агента
# проверяет его между шагами (кооперативная остановка — LLM-вызов
# в полёте прервать нельзя, дожидаемся его ответа).
STOP = threading.Event()

# Human-in-the-loop подтверждение деструктивных вызовов (этап
# защиты): run_agent с вердиктом "confirm" блокируется на
# CONFIRM["event"], UI шлёт POST /api/confirm {approve: bool}.
# Pending принадлежит одному прогону (RUN_LOCK — прогон один),
# event одноразовый: повторный POST не исполнит вызов дважды.
CONFIRM = {"event": threading.Event(), "approve": False, "request_id": None, "pending": False}
CONFIRM_LOCK = threading.Lock()
CONFIRM_TIMEOUT_SEC = 90          # таймаут ответа = отказ (fail-closed)


def _ui_confirm(call: dict) -> bool:
    """Блокирующее ожидание решения человека из UI.
    Stop во время ожидания тоже = отказ."""
    answered = CONFIRM["event"].wait(timeout=CONFIRM_TIMEOUT_SEC)
    with CONFIRM_LOCK:
        approved = bool(answered and CONFIRM["approve"] and not STOP.is_set())
        CONFIRM["pending"] = False
        CONFIRM["request_id"] = None
        return approved


def _run(task: str,
         headed: bool, emit, cdp: str = "", target: str = "",
         analyze: bool = False) -> None:
    # Разведка страницы (кнопка «Разведать» в UI): read-only снапшот →
    # черновик политики. Task не нужен — берём URL из поля target.
    if analyze:
        url = extract_url(target) or extract_url(task)
        if not url:
            emit({"phase": "error", "message":
                  "задайте страницу в поле наверху"})
            emit({"phase": "done", "report": {"status": "error"}})
            return
        try:
            llm = get_llm()
            RUN_LLM["llm"] = llm
            # Дельта токенов: tokens_used — накопительный счётчик по
            # всему процессу, а для итога нужен расход этого прогона.
            tok0 = getattr(llm, "tokens_used", 0)
            draft = analyze_site(url, llm, headed=headed, cdp=cdp,
                                 on_event=emit)
            emit({"phase": "llm_prompt", "step": 0,
                  "prompt": getattr(llm, "last_prompt", "")})
        except Exception as e:                               # noqa: BLE001
            emit({"phase": "error", "message": str(e)})
            emit({"phase": "done", "report": {"status": "error"}})
            return
        # Итог с метаданными, как у обычного прогона: разведка — это
        # тоже LLM-вызов (один), и на CPU он недешёвый — полезно видеть.
        emit({"phase": "done", "report": {
            "status": "analyzed",
            "llm_calls": 1,
            "tokens": getattr(llm, "tokens_used", 0) - tok0,
            "answer": json.dumps(draft, ensure_ascii=False, indent=2)}})
        return

    # «Поле наверху» (target) — единственный источник целевой страницы:
    # ссылка в тексте задачи scope НЕ меняет (иначе «зайди на x.com»
    # переключало бы сайт мимо видимой политики — её отклонит
    # check_task как чужой домен). Поле пустое → бутстрап ссылкой из
    # первого сообщения.
    url_in_task = extract_url(task)
    url = target or url_in_task
    if not url:
        emit({"phase": "error", "message":
              "задайте страницу в поле наверху (https://…) — "
              "от неё строится scope"})
        emit({"phase": "done", "report": {"status": "error"}})
        return
    scenario = scenario_for_url(url)
    # Источник URL различаем в логе: поле наверху или ссылка-бутстрап.
    source = "поле наверху" if target else "ссылка в сообщении"
    # Какая политика реально действует — видно в ленте, а не только
    # в логе: студентская запись из POLICIES или дефолт (всё deny).
    try:
        from student_policy import POLICIES
        policy_src = (f"студенческая ({len(POLICIES[scenario.name])} правил)"
                      if scenario.name in POLICIES
                      else "дефолт: всё deny")
    except ImportError:
        policy_src = "дефолт: всё deny"
    # Сценарий + действующая политика — постоянный баббл (фаза
    # "scenario"), а не status: транзиентная строка перезаписывалась
    # следующим status и стиралась clearStatus() — в чате не было
    # видно, какой сценарий/scope применился к прогону.
    emit({"phase": "scenario", "name": scenario.name,
          "policy": policy_src, "read_only": scenario.read_only,
          "deny_tools": list(scenario.deny_tools),
          "domains": scenario.domains,
          "url": url, "source": source})
    # «Продолжи»: если прошлая сессия была на этом же домене —
    # подсекаем её историю, модель увидит контекст.
    resume = None
    if target:
        sess = load_session()
        if sess and sess.get("url") and scenario.domains and \
                any(d in sess["url"] for d in scenario.domains):
            resume = sess
            emit({"phase": "status", "message":
                  "продолжаю прошлую сессию с контекстом"})
    llm = get_llm()
    RUN_LLM["llm"] = llm
    STOP.clear()
    run_agent(task or scenario.example_task, llm, scenario, headed=headed,
              on_event=emit, cdp=cdp, stop_event=STOP, resume=resume,
              step_delay=HEADED_STEP_DELAY if headed else 0.0,
              confirm_fn=_ui_confirm)


# Итог последнего прогона: UI дочитывает его через GET /api/report,
# если SSE-стрим оборвался посреди прогона (закрытая вкладка, рестарт
# vite). Без этого ответ пропадал — прогон на бэкенде доходил до конца,
# а UI видел только «соединение оборвалось».
LAST: dict = {"task": None, "report": None, "ts": 0.0}
# Кольцевой буфер всех событий прогона: прогон может стартовать
# из curl/другого окна — UI дочитывает поток через /api/events.
import collections
EVENTS: "collections.deque[dict]" = collections.deque(maxlen=2000)
SEQ = [0]

# Живой прогресс текущего прогона — GET /api/status. UI опрашивает его
# вместе с /api/report после обрыва стрима, чтобы ожидание итога
# показывало реальные счётчики (шаги/LLM/токены), а не мёртвое «жду».
CURR: dict = {"running": False}
# LLM текущего прогона: get_llm() — фабрика, каждый вызов возвращает
# НОВЫЙ экземпляр с tokens_used=0 — поэтому живой счётчик токенов
# читаем с объекта прогона, а не со свежего (было: вечные «~0 tok»).
RUN_LLM: dict = {"llm": None}

# Персистентность истории прогона: рестарт serve.py обнуляет EVENTS/
# LAST в памяти, а UI по BOOT_ID чистит чат — без файла последний
# прогон пропадал бы навсегда. Пишем на диск и поднимаем при старте.
HISTORY_FILE = ROOT / "last_run.json"


def _save_history() -> None:
    """События прогона + итог на диск — см. HISTORY_FILE."""
    try:
        HISTORY_FILE.write_text(json.dumps(
            {"events": list(EVENTS), "last": LAST},
            ensure_ascii=False), encoding="utf-8")
    except OSError:
        # Диск недоступен — история теряется, прогон не должен падать.
        pass


def _load_history() -> None:
    """Восстановить последний прогон после рестарта — см. _save_history."""
    try:
        data = json.loads(HISTORY_FILE.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return
    for ev in data.get("events", []):
        EVENTS.append(ev)
        SEQ[0] = max(SEQ[0], int(ev.get("_seq", 0)))
    LAST.update(data.get("last") or {})


class Handler(http.server.SimpleHTTPRequestHandler):
    def log_message(self, *a):
        pass

    def _json(self, obj, code=200):
        body = json.dumps(obj, ensure_ascii=False).encode()
        self.send_response(code)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        # CORS: UI ходит напрямую :5173 → :8090 без vite-прокси —
        # прокси ронял SSE-стрим при обрыве вкладки (EPIPE).
        self.send_header("Access-Control-Allow-Origin", "*")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _sse(self):
        self.send_response(200)
        self.send_header("Content-Type", "text/event-stream")
        self.send_header("Cache-Control", "no-cache")
        # «Connection: close» — честное обрамление: ответ HTTP/1.0 без
        # Content-Length и без chunked валиден только как read-until-
        # close; «keep-alive» обещал переиспользование сокета, которого
        # не будет — по этой двусмысленности Necko (Firefox) мог убивать
        # транзакцию посреди прогона.
        self.send_header("Connection", "close")
        self.send_header("Access-Control-Allow-Origin", "*")
        self.end_headers()

        wlock = threading.Lock()
        stop_ping = threading.Event()
        # Сокет умер (F5/закрытая вкладка/сеть): emit → no-op, но прогон
        # НЕ глушим — итог сохранится в LAST и дочитается через
        # /api/report, когда UI вернётся.
        dead = threading.Event()
        t0 = time.monotonic()        # метка открытия стрима — для логов
        dropped = [0]                # события, ушедшие в мёртвый сокет

        def _write(raw: bytes):
            with wlock:
                self.wfile.write(raw)
                self.wfile.flush()

        def _mark_dead(where: str,
                       exc: BaseException | None = None) -> None:
            if not dead.is_set():
                # Главный диагностический лог разрыва: ПЕРВЫЙ неудачный
                # write — момент, когда клиент порвал SSE. ECONNRESET/
                # EPIPE на ping в тишине = клиент ушёл (reload вкладки,
                # HMR, закрытие), а не мы закрыли сокет.
                detail = (f": {type(exc).__name__}: {exc}"
                          if exc is not None else "")
                print(f"[serve +{time.monotonic() - t0:6.1f}s] SSE-сокет "
                      f"мёртв ({where}){detail} — клиент отвалился, "
                      f"прогон продолжается, итог уйдёт в /api/report",
                      file=sys.stderr, flush=True)
            dead.set()

        def emit(ev: dict):
            if dead.is_set():
                # Прогон ещё шлёт события в никуда — считаем потери,
                # чтобы видеть, сколько работы прошло мимо UI.
                dropped[0] += 1
                if dropped[0] in (1, 10, 50):
                    print(f"[serve] SSE мёртв — событий потеряно: "
                          f"{dropped[0]}", file=sys.stderr, flush=True)
                return
            try:
                _write(f"data: {json.dumps(ev, ensure_ascii=False)}\n\n"
                       .encode())
            except DEAD_SOCKET as e:
                _mark_dead(f"emit:{ev.get('phase')}", e)

        def _ping():
            # SSE-комментарий: клиент игнорирует, но байты держат коннект
            # и обнаруживают отвалившегося клиента. Мёртвый сокет → emit
            # глушим, но прогон доживает — итог заберут через /api/report.
            while not stop_ping.wait(PING_EVERY_SEC):
                try:
                    _write(b": ping\n\n")
                except DEAD_SOCKET as e:
                    _mark_dead("ping", e)
                    return

        pinger = threading.Thread(target=_ping, daemon=True)
        pinger.start()
        emit.pinger = pinger                      # noqa: attr-defined
        emit.stop_ping = stop_ping                # noqa: attr-defined
        # Флаги для сводки в finally do_POST: умер ли сокет и сколько
        # событий прогона ушло в никуда.
        emit.dead = dead                          # noqa: attr-defined
        emit.dropped = dropped                    # noqa: attr-defined
        return emit

    def do_OPTIONS(self):
        # CORS-preflight: POST /api/run с Content-Type: application/json
        # с чужого origin (:5173) требует OPTIONS до самого запроса.
        self.send_response(204)
        self.send_header("Access-Control-Allow-Origin", "*")
        self.send_header("Access-Control-Allow-Methods",
                         "GET, POST, OPTIONS")
        self.send_header("Access-Control-Allow-Headers", "Content-Type")
        self.send_header("Content-Length", "0")
        self.end_headers()

    def do_GET(self):
        if self.path == "/api/report":
            return self._json(LAST)
        if self.path.startswith("/api/events"):
            from urllib.parse import parse_qs, urlparse
            q = parse_qs(urlparse(self.path).query)
            after = int((q.get("after") or ["0"])[0])
            evs = [e for e in EVENTS if e.get("_seq", 0) > after]
            return self._json({"events": evs, "running":
                               CURR.get("running", False),
                               "boot": BOOT_ID})

        if self.path == "/api/status":
            return self._json(CURR)
        if self.path in ("/", ""):
            # UI больше не раздаётся статикой — он живёт на vite
            # dev-сервере :5173 (проксирует /api сюда).
            return self._json({
                "hint": "UI: cd frontend && npm run dev → "
                        "http://localhost:5173"})
        return self._json({"error": "not found"}, 404)

    def do_POST(self):
        length = int(self.headers.get("Content-Length", 0))
        try:
            body = json.loads(self.rfile.read(length) or b"{}")
        except json.JSONDecodeError:
            return self._json({"error": "bad json"}, 400)

        if self.path == "/api/stop":
            STOP.set()
            CONFIRM["event"].set()      # стоп будит и confirm-ожидание
            # «Стоп» сильнее кооперативного флага между шагами: дёргаем
            # и сам LLM-вызов — abort() закрывает HTTP к Ollama, демон
            # обрывает генерацию в полёте (иначе ждали бы 30–180 с).
            abort = getattr(RUN_LLM.get("llm"), "abort", None)
            if abort:
                abort()
            return self._json({"ok": True})

        if self.path == "/api/clear":
            # «Новый чат» в UI = ПОЛНЫЙ сброс состояния, не только лента:
            # EVENTS иначе реплеит старый прогон на первый же /api/events
            # после F5, а session.json вернёт контекст «продолжи».
            STOP.set()                  # живой прогон — кооперативный стоп
            CONFIRM["event"].set()      # будим возможный confirm-ожидатель
            abort = getattr(RUN_LLM.get("llm"), "abort", None)
            if abort:
                abort()                 # см. /api/stop: генерация тоже рвётся
            EVENTS.clear()
            # SEQ НЕ сбрасываем: он монотонный, а lastSeq во фронте
            # остаётся валидным — иначе после сброса события нового
            # прогона (с _seq < старого lastSeq) отфильтруются поллером.
            LAST.update(task=None, report=None, ts=0.0)
            CURR.clear()
            CURR["running"] = False
            try:
                # session.json — «продолжи»-контекст; last_run.json —
                # история прогона: иначе «Новый чат» воскреснет при
                # рестарте через _load_history.
                (ROOT / "session.json").unlink(missing_ok=True)
                HISTORY_FILE.unlink(missing_ok=True)
            except OSError as e:
                print(f"[serve] сброс файлов состояния: {e}",
                      file=sys.stderr, flush=True)
            return self._json({"ok": True})

        if self.path == "/api/confirm":
            # Approve/Deny из модалки: будим _ui_confirm ровно один раз.
            with CONFIRM_LOCK:
                if not CONFIRM["pending"] or body.get("request_id") != CONFIRM["request_id"]:
                    return self._json({"error": "no matching pending confirmation"}, 409)
                if type(body.get("approve")) is not bool:
                    return self._json({"error": "approve must be boolean"}, 400)
                CONFIRM["approve"] = body["approve"]
                CONFIRM["pending"] = False
                CONFIRM["event"].set()
            return self._json({"ok": True})

        if self.path == "/api/run":
            # Один прогон на бэкенд: STOP/RUN_LLM/CONFIRM — общее
            # состояние, второй run перетирал бы их первому
            # (STOP.clear() снимал чужой «стоп», RUN_LLM перехватывал
            # abort-ручку). Живой прогон + новый запрос → 409.
            if CURR.get("running"):
                return self._json(
                    {"error": "прогон уже идёт — дождитесь итога "
                              "или остановите его (/api/stop)"}, 409)
            emit = self._sse()
            # Обёртка: перехватываем финальный done → LAST для
            # /api/report (доставка итога при оборванном стриме).
            raw_emit = emit
            done_sent = [False]     # флаг для сводки в finally ниже

            def emit(ev: dict):                        # noqa: F811
                ph = ev.get("phase")
                if ph == "confirm":
                    import uuid
                    with CONFIRM_LOCK:
                        CONFIRM.update(approve=False, pending=True, request_id=uuid.uuid4().hex)
                        CONFIRM["event"].clear()
                        ev["request_id"] = CONFIRM["request_id"]
                        ev["timeout_seconds"] = CONFIRM_TIMEOUT_SEC
                SEQ[0] += 1
                ev["_seq"] = SEQ[0]
                EVENTS.append(dict(ev))
                if ph == "start":
                    CURR.update(running=True, task=ev.get("task"),
                                steps=0, llm_calls=0, tokens=0,
                                status="запуск",
                                start_ts=time.time())
                elif ph == "thinking":
                    CURR["status"] = (f"модель думает… "
                                      f"(шаг {ev.get('step', 0)})")
                elif ph == "think":
                    # think идёт после каждого decide — считаем вызовы.
                    CURR["llm_calls"] = CURR.get("llm_calls", 0) + 1
                elif ph == "step":
                    CURR["steps"] = ev.get("step", 0) + 1
                    CURR["status"] = (f"шаг {ev.get('step', 0)}: "
                                      f"{(ev.get('decision') or {}).get('tool', '?')}")
                elif ph == "status":
                    CURR["status"] = ev.get("message", "")
                elif ph == "done":
                    done_sent[0] = True
                    CURR["running"] = False
                    LAST["task"] = body.get("task")
                    # Длительность прогона — в отчёт итога («итог: …, 92с»).
                    rep = ev.get("report") or {}
                    start_ts = CURR.get("start_ts")
                    if start_ts:
                        rep["elapsed_s"] = int(time.time() - start_ts)
                    LAST["report"] = rep
                    LAST["ts"] = time.time()
                    _save_history()
                # Токены — накопительный счётчик OllamaLLM прогона
                # (см. RUN_LLM: фабрика get_llm() давала бы вечный 0);
                # между шагами показывает расход по последнему вызову.
                if ph != "done":
                    CURR["tokens"] = getattr(
                        RUN_LLM["llm"], "tokens_used", 0)
                return raw_emit(ev)

            # _sse() вешает на свой emitter служебные атрибуты, которыми
            # пользуется finally — пробрасываем их на обёртку, иначе
            # AttributeError при закрытии стрима.
            emit.stop_ping = raw_emit.stop_ping        # noqa: attr-defined
            emit.pinger = raw_emit.pinger              # noqa: attr-defined
            emit.dead = raw_emit.dead                  # noqa: attr-defined
            emit.dropped = raw_emit.dropped            # noqa: attr-defined

            busy = not RUN_LOCK.acquire(blocking=False)
            # Диагностика разрывов: finished/done_sent в finally отличают
            # «_run упал» от «вкладка отвалилась, а прогон жил дальше».
            t_run = time.monotonic()
            finished = False
            print(f"[serve] run begin: task={body.get('task')!r} "
                  f"target={body.get('target')!r} "
                  f"analyze={bool(body.get('analyze'))}",
                  file=sys.stderr, flush=True)
            try:
                if busy:
                    emit({"phase": "error", "message":
                          "агент уже выполняет задачу — дождитесь итога"})
                    emit({"phase": "done",
                          "report": {"status": "busy"}})
                    return
                emit({"phase": "start", "task": body.get("task")})
                # Прогон в этом же потоке-обработчике: ThreadingHTTPServer
                # даёт каждому SSE-коннекту свой поток.
                _run(body.get("task", ""),
                     bool(body.get("headed")),
                     emit,
                     # CDP-подключение к живому Chrome: поле "cdp" в
                     # POST или env CDP_ENDPOINT у процесса serve.py.
                     body.get("cdp") or
                     os.environ.get("CDP_ENDPOINT", ""),
                     # «Поле наверху» в UI: текущая целевая страница —
                     # нужно для follow-up сообщений без ссылки.
                     str(body.get("target") or ""),
                     bool(body.get("analyze")))
                finished = True
            except ClientGone:
                print("[serve] client disconnected mid-run", flush=True)
            except Exception as e:                       # noqa: BLE001
                traceback.print_exc()
                try:
                    emit({"phase": "error", "message": str(e)})
                except ClientGone:
                    pass
            except BaseException as e:
                # SystemExit/KeyboardInterrupt в worker-потоке — редко,
                # но без лога это «немая» смерть прогона: клиент видит
                # только обрыв стрима без error- и done-событий.
                print(f"[serve] run aborted by {type(e).__name__}: {e}",
                      file=sys.stderr, flush=True)
                traceback.print_exc()
                raise
            finally:
                emit.stop_ping.set()                     # noqa: attr-defined
                if not busy:
                    RUN_LOCK.release()
                # Сводка прогона — по ней читается расклад: finished=False
                # → смотреть traceback выше; sse_dead=True → клиент ушёл
                # раньше конца (см. «SSE-сокет мёртв» выше с причиной).
                print(f"[serve] run end +{time.monotonic() - t_run:.1f}s: "
                      f"finished={finished} done_sent={done_sent[0]} "
                      f"sse_dead={emit.dead.is_set()} "  # noqa: attr-defined
                      f"dropped={emit.dropped[0]}",      # noqa: attr-defined
                      file=sys.stderr, flush=True)
                # SSE-ответ без Content-Length завершается только
                # закрытием сокета: без этого http.server держит
                # keep-alive соединение открытым и клиент висит/рвётся
                # с "Error in input stream".
                self.close_connection = True
            return

        return self._json({"error": "unknown endpoint"}, 404)


DEFAULT_CDP = "http://localhost:9222"


# ---------------------------------------------------------------------------
# Авто-рестарт: занятый порт и демон Ollama
# ---------------------------------------------------------------------------
from port_guard import ensure_port_free


def _ollama_alive(url: str) -> bool:
    """Пинг демона Ollama: /api/tags — дешёвый healthcheck."""
    try:
        with urllib.request.urlopen(f"{url}/api/tags", timeout=2):
            return True
    except OSError:
        return False


def _ensure_ollama() -> None:
    """Гарантировать живой демон Ollama при старте backend.

    Убивать демон при рестарте НЕ нужно: он переживает serve.py, а
    модель в нём уже разогрета — каждый kill стоил бы перезагрузку
    ~2.6 GB. Поэтому семантика «ensure alive», а не «kill & restart»:
    молчит → поднимаем `ollama serve` detached; жив → не трогаем."""
    url = os.environ.get("OLLAMA_URL", "http://localhost:11434")
    if urlparse(url).hostname not in ("localhost", "127.0.0.1", "::1"):
        print(f"[ollama] внешний {url} — автозапуск не применяется",
              flush=True)
        return
    if _ollama_alive(url):
        print(f"[ollama] демон жив: {url}", flush=True)
        return
    print(f"[ollama] {url} молчит — запускаю `ollama serve`…",
          file=sys.stderr, flush=True)
    log_dir = ROOT / "logs"
    log_dir.mkdir(exist_ok=True)
    try:
        log = open(log_dir / "ollama_serve.log", "ab")
        # detached от backend-процесса: демон должен пережить рестарт
        subprocess.Popen(["ollama", "serve"], stdout=log, stderr=log,
                         stdin=subprocess.DEVNULL, start_new_session=True)
    except FileNotFoundError:
        print("[ollama] команда `ollama` не найдена — установите "
              "(ollama.com) или запустите `ollama serve` вручную",
              file=sys.stderr, flush=True)
        return
    deadline = time.monotonic() + 20.0
    while time.monotonic() < deadline:
        if _ollama_alive(url):
            print(f"[ollama] демон поднялся: {url}", flush=True)
            return
        time.sleep(0.3)
    print(f"[ollama] демон не ответил за 20 с — см. "
          f"{log_dir / 'ollama_serve.log'}",
          file=sys.stderr, flush=True)


def main() -> None:
    p = argparse.ArgumentParser()
    p.add_argument("--port", type=int, default=8090)
    p.add_argument("--cdp", default=None, metavar="URL",
                   help="CDP endpoint живого Chrome "
                        f"(default {DEFAULT_CDP}; env CDP_ENDPOINT)")
    p.add_argument("--no-cdp", action="store_true",
                   help="чистый браузер без CDP (без ваших логинов; "
                        "headless если нет HEADED=1)")
    args = p.parse_args()
    # Авто-рестарт ДО поднятия Chrome: если порт держит прежний
    # serve.py — гасим его и занимаем порт; чужой слушатель → выход с
    # понятной ошибкой (см. port_guard.ensure_port_free).
    if not ensure_port_free(args.port, owns=("serve.py",),
                            tag="serve"):
        sys.exit(1)
    _ensure_ollama()
    # CDP — режим по умолчанию: server.py сам поднимет Chrome с
    # remote-debugging на профиле /tmp/agent-chrome, логины живут там.
    if args.no_cdp:
        os.environ["CDP_ENDPOINT"] = ""
    else:
        os.environ.setdefault("CDP_ENDPOINT", args.cdp or DEFAULT_CDP)
        # Живой Chrome открываем сразу, а не лениво на первом вызове —
        # пользователь видит окно и может залогиниться до старта агента.
        from server import ensure_cdp_chrome
        try:
            ensure_cdp_chrome(os.environ["CDP_ENDPOINT"])
        except Exception as e:                               # noqa: BLE001
            print(f"[cdp] Chrome не поднялся: {e}", file=sys.stderr)
    httpd = http.server.ThreadingHTTPServer(("127.0.0.1", args.port), Handler)
    print(f"Browser-agent API: http://localhost:{args.port}/ "
          f"(UI — cd frontend && npm run dev → :5173)")
    _load_history()
    if EVENTS:
        print(f"[serve] история: восстановлено {len(EVENTS)} событий прошлого прогона", flush=True)
    httpd.serve_forever()


if __name__ == "__main__":
    main()
