/** main.ts — композиция: DOM + SSE-поток событий агента.
 *
 * Целевая страница задаётся ссылкой в первом сообщении и хранится
 * в sessionStorage как «поле наверху» — пока пользователь не сбросит
 * её кнопкой ×, все следующие задачи идут на этот сайт.
 */
import "./styles.css";
import { API_BASE, streamRun, pollReport,
         type AgentEvent, type Snapshot } from "./api";
import { applyEvent, initialState, DEFAULT_TOOLS,
         type RunState } from "./state";


const $ = <T extends HTMLElement>(id: string): T => {
  const el = document.getElementById(id);
  if (!el) throw new Error(`#${id} not found`);
  return el as T;
};

const chat = $<HTMLDivElement>("chat");
// headed всегда включён: демо должно показывать реальный браузер.
// Модель — только Ollama (единственный бэкенд).
const HEADED = true;
const LLM = "ollama";
const taskIn = $<HTMLTextAreaElement>("task");
const sendBtn = $<HTMLButtonElement>("send");
const stopBtn = $<HTMLButtonElement>("stop");
const statsEl = $<HTMLElement>("stats");
const toolsEl = $<HTMLUListElement>("tools");
const snapshotEl = $<HTMLPreElement>("snapshot");
const targetInput = $<HTMLInputElement>("targetInput");
const targetClear = $<HTMLButtonElement>("targetClear");
const analyzeBtn = $<HTMLButtonElement>("analyze");
const snapCopy = $<HTMLButtonElement>("snapCopy");
const snapDl = $<HTMLButtonElement>("snapDl");
const clearBtn = $<HTMLButtonElement>("clearChat");
const confirmModal = $<HTMLDivElement>("confirmModal");
const confirmText = $<HTMLParagraphElement>("confirmText");
const confirmCall = $<HTMLPreElement>("confirmCall");
const confirmYes = $<HTMLButtonElement>("confirmYes");
const confirmNo = $<HTMLButtonElement>("confirmNo");
let confirmationId: string | undefined;

const esc = (s: unknown): string =>
  String(s).replace(/[&<>]/g, (c) =>
    ({ "&": "&amp;", "<": "&lt;", ">": "&gt;" })[c] as string);

/* URL приходит percent-encoded — кириллица в ru.wikipedia и Drive
   нечитаема (%D0%9F…). Для ПОКАЗА декодируем в Unicode; сама ссылка
   для навигации не меняется. Битое кодирование — показываем как есть,
   ронять рендер из-за одного URL нельзя. */
const decodeUrl = (u: string): string => {
  try {
    return decodeURIComponent(u);
  } catch {
    return u;
  }
};

/* --- персистентность: чат и панель переживают перезагрузку --------------- */
const CHAT_KEY = "ba-chat";
const STATE_KEY = "ba-run-state";
// Метка процесса бэкенда (serve.py BOOT_ID): рестарт обнуляет серверную
// историю — чат из старого процесса становится фантомом, надо чистить.
// BOOT_ID из /api/events — только для отладки (рестарт бэкенда чат
// НЕ чистит: история живёт в sessionStorage + last_run.json).
// Диагностика обрывов SSE: фиксируем уход страницы. Если прогон был в
// полёте, fetch умер вместе с JS — обрыв «свой» (reload/HMR при правках
// src/), а не серверный. Маркер читает restore().
const PAGEHIDE_KEY = "ba-pagehide";
window.addEventListener("pagehide", () => {
  try {
    sessionStorage.setItem(PAGEHIDE_KEY, String(Date.now()));
  } catch { /* storage недоступен — маркер необязателен */ }
});

function persist(): void {
  try {
    sessionStorage.setItem(CHAT_KEY, chat.innerHTML);
    sessionStorage.setItem(STATE_KEY, JSON.stringify({
      ...state,
      usedTools: Object.fromEntries(state.usedTools),
      blockedTools: [...state.blockedTools],
      // «Прогон в полёте»: restore() по этой паре возобновляет опрос
      // /api/report после перезагрузки вкладки.
      awaiting: awaitingReport,
      runStart: runStartTs,
    }));
  } catch {
    /* sessionStorage переполнен — просто не сохраняем дальше */
  }
}

function restore(): void {
  const savedChat = sessionStorage.getItem(CHAT_KEY);
  const savedState = sessionStorage.getItem(STATE_KEY);
  let resumeAt = 0;   // unix-секунды старта прогона, ждущего итог
  if (savedState) {
    try {
      const raw = JSON.parse(savedState);
      state = {
        ...initialState(), ...raw,
        usedTools: new Map(Object.entries(raw.usedTools ?? {})),
        blockedTools: new Set(raw.blockedTools ?? []),
      };
      // Старые записи могли сохраниться с пустым каталогом — подставляем
      // дефолт, свежий tools/list всё равно перезапишет его на прогоне.
      if (!state.tools.length) state.tools = [...DEFAULT_TOOLS];
      if (raw.awaiting && typeof raw.runStart === "number") {
        resumeAt = raw.runStart;
      }
    } catch { /* битый JSON — начинаем с чистого состояния */ }
  }
  if (savedChat) {
    chat.innerHTML = savedChat;
    // «Модель думает…» — транзиентные бабблы; после перезагрузки стрим
    // мёртв, так что статусные строки убираем, а прогон помечаем.
    const stale = chat.querySelectorAll(".msg--status");
    stale.forEach((el) => el.remove());
    chat.scrollTop = chat.scrollHeight;
    if (resumeAt) {
      // pagehide-маркер новее старта прогона → разрыв вызван уходом
      // страницы (ручной reload или полный reload от vite HMR), а не
      // падением бэкенда — прогон на нём доживает.
      const pagehideAt = Number(
        sessionStorage.getItem(PAGEHIDE_KEY) ?? 0) / 1000;
      bubble(pagehideAt >= resumeAt
        ? "страница была перезагружена во время прогона (HMR/вручную) " +
          "— прогон продолжается на сервере, жду итог…"
        : "страница перезагружена — прогон продолжается на сервере, " +
          "жду итог…");
      void resumePolling(resumeAt);
    } else if (stale.length) {
      bubble("страница перезагружена — прогон был прерван");
    }
    sessionStorage.removeItem(PAGEHIDE_KEY);
  }
}

// Поллер: прогоны, начатые не из этого окна (curl, другая вкладка),
// дочитываются из буфера сервера — промпты видны всегда.
setInterval(async () => {
  try {
    const r = await fetch(`/api/events?after=${lastSeq}`);
    const j = await r.json() as { events?: AgentEvent[] };
    for (const ev of j.events || []) renderEvent(ev);
  } catch { /* backend оффлайн */ }
}, 2500);

function bubble(html: string, cls = "sys"): HTMLDivElement {
  const d = document.createElement("div");
  d.className = `msg msg--${cls}`;
  d.innerHTML = html;
  chat.appendChild(d);
  chat.scrollTop = chat.scrollHeight;
  persist();
  return d;
}

/** Порог схлопывания: тексты длиннее него уходят в <details>-блок,
 *  короткие остаются обычными бабблами в потоке чата. */
const COLLAPSE_LEN = 280;

/** Схлопнутый лог-блок для длинных текстов (reasoning модели,
 *  результаты инструментов): шапка-<summary> + тело-<pre>, закрыт
 *  по умолчанию — тот же пунктирный блок, что и llm_prompt. */
function foldBubble(summaryHtml: string, bodyText: string, cls: string): void {
  // НЕ <details>/<summary>: элемент details рвёт drag-выделение ленты
  // на своей границе (Chrome) — нельзя скопировать чат целиком.
  // Обычный div + [hidden]-переключатель выглядит так же, но весь
  // текст — нормально выделяемые элементы.
  const d = document.createElement("div");
  d.className = `msg msg--${cls} msg--fold`;
  const head = document.createElement("div");
  head.className = "prompt-head";
  head.innerHTML = summaryHtml;
  const pre = document.createElement("pre");
  pre.textContent = bodyText;
  pre.hidden = true;
  head.addEventListener("click", () => {
    pre.hidden = !pre.hidden;
    head.classList.toggle("prompt-head--open", !pre.hidden);
    persist();
  });
  d.append(head, pre);
  chat.append(d);
  chat.scrollTop = chat.scrollHeight;
  persist();
}

/* --- целевая страница (отдельное поле + список предложенных сайтов) -- */
const URL_RE = /https?:\/\/[^\s«»"'<>()]+/;
const TARGET_KEY = "ba-target-url";

let targetUrl = sessionStorage.getItem(TARGET_KEY) ?? "";

function renderTarget(): void {
  // Не затираем то, что пользователь прямо сейчас печатает в поле.
  if (document.activeElement !== targetInput) {
    targetInput.value = targetUrl;
  }
  targetClear.hidden = !targetUrl;
}

function setTarget(url: string, announce = false): void {
  // Панель — как prompt cache: снапшот и каталог инструментов живут,
  // пока страница та же; смена target их инвалидирует — показывать
  // eN-refs и tools ЧУЖОЙ страницы было бы обманом.
  if (url !== targetUrl) {
    state.snapshot = null;
    state.tools = [];
    renderPanel();
  }
  targetUrl = url;
  if (url) {
    sessionStorage.setItem(TARGET_KEY, url);
  } else {
    sessionStorage.removeItem(TARGET_KEY);
  }
  renderTarget();
  if (announce) {
    bubble(url
      ? `целевая страница: ${esc(url)}`
      : "страница сброшена — задайте URL в поле наверху");
  }
}

// Смена цели — только по явному выбору из списка/Enter, не на каждый
// введённый символ: незаконченный URL не должен трогать sessionStorage.
targetInput.addEventListener("change", () =>
  setTarget(targetInput.value.trim(), true));
targetClear.addEventListener("click", () => {
  setTarget("");
  bubble("страница сброшена — задайте URL в поле наверху");
});

/* --- боковая панель ------------------------------------------------------ */
let state: RunState = initialState();

function renderStats(): void {
  const rows: Array<[string, string]> = [
    ["статус", state.status],
    ["шагов", String(state.steps)],
    ["вызовов LLM", String(state.llmCalls)],
    ["токенов", String(state.tokens)],
    ["ошибок verify", String(state.verifyFailures)],
    ["scope-отказов", String(state.scopeViolations)],
  ];
  statsEl.innerHTML = rows
    .map(([k, v]) => `<dt>${esc(k)}</dt><dd>${esc(v)}</dd>`)
    .join("");
}

function renderTools(): void {
  if (!state.tools.length) {
    toolsEl.innerHTML = `<li class="unused">tools/list ещё не получен</li>`;
    return;
  }
  const names = [...state.tools, "task_done"];
  toolsEl.innerHTML = names
    .map((n) => {
      const used = state.usedTools.get(n) ?? 0;
      const cls = state.blockedTools.has(n)
        ? "blocked"
        : used > 0
          ? "used"
          : "unused";
      const mark = state.blockedTools.has(n)
        ? " ⛔"
        : used > 0
          ? ` ×${used}`
          : "";
      return `<li class="${cls}">${esc(n)}${mark}</li>`;
    })
    .join("");
}

/** Снапшот целиком — все элементы + текст, панель прокручивается. */
function renderSnapshot(s: Snapshot | null): void {
  if (!s) {
    snapshotEl.textContent = "—";
    return;
  }
  const els = s.elements
    .map((e) =>
      `<div class="snap__el"><code>${esc(e.ref)}</code> ` +
      `<span class="snap__role">${esc(e.role)}</span> ` +
      `${esc(e.name ?? "")}` +
      (e.input_type ? ` <span class="snap__meta">type=${esc(e.input_type)}</span>` : "") +
      // group — имя радио-группы, checked — активный вариант (см.
      // server.py _SNAPSHOT_JS: radio Wikipedia без этого был «0/1/2»).
      (e.group ? ` <span class="snap__meta">группа: ${esc(e.group)}</span>` : "") +
      (e.checked !== undefined ? ` <span class="snap__meta">${e.checked ? "☑" : "☐"}</span>` : "") +
      (e.value ? ` <span class="snap__meta">= «${esc(String(e.value))}»</span>` : "") +
      (e.href ? `<div class="snap__href">→ ${esc(decodeUrl(e.href))}</div>` : "") +
      `</div>`)
    .join("");
  snapshotEl.innerHTML =
    `<div class="snap__head"><b>${esc(s.title || "(no title)")}</b>` +
      `<div class="snap__href">${esc(decodeUrl(s.url ?? ""))}</div></div>` +
    (s.lead_text
      ? `<div class="snap__sec">lead</div>` +
        `<div class="snap__text">${esc(s.lead_text)}</div>`
      : "") +
    (s.main_text
      ? `<div class="snap__sec">текст</div>` +
        `<div class="snap__text">${esc(s.main_text)}</div>`
      : "") +
    `<div class="snap__sec">элементы (${s.elements.length})</div>${els}`;
}

function renderPanel(): void {
  renderStats();
  renderTools();
  renderSnapshot(state.snapshot);
}

snapCopy.addEventListener("click", async () => {
  if (!state.snapshot) return;
  await navigator.clipboard.writeText(
    JSON.stringify(state.snapshot, null, 2));
  snapCopy.textContent = "скопировано";
  setTimeout(() => (snapCopy.textContent = "копировать"), 1500);
});

snapDl.addEventListener("click", () => {
  if (!state.snapshot) return;
  const blob = new Blob([JSON.stringify(state.snapshot, null, 2)],
    { type: "application/json" });
  const a = document.createElement("a");
  a.href = URL.createObjectURL(blob);
  a.download = "snapshot.json";
  a.click();
  URL.revokeObjectURL(a.href);
});

/* --- события агента в чат ------------------------------------------------ */
let statusEl: HTMLDivElement | null = null;

function setStatus(text: string): void {
  if (!statusEl) statusEl = bubble("", "status");
  statusEl.innerHTML = `<b class="pulse">●</b> ${esc(text)}`;
}

function clearStatus(): void {
  statusEl?.remove();
  statusEl = null;
}

/** Ответ агента: если строка — валидный JSON (отчёт «Разведать» кладёт
 * в answer весь {report, policy}), нормализуем отступы и показываем
 * сырым JSON в <pre>, иначе innerHTML схлопнет переносы в одну строку.
 * Обычный текстовый ответ проходит тем же <pre> без изменений. */
function answerHtml(answer: string): string {
  let text = answer;
  try {
    text = JSON.stringify(JSON.parse(answer), null, 2);
  } catch {
    /* не JSON — обычный текстовый ответ, оставляем как есть */
  }
  return `<b>Ответ агента:</b><pre class="msg__pre">${esc(text)}</pre>`;
}

let lastSeq = 0;
let lastLocalTask = "";   // задача, введённая в ЭТОМ окне
// done может прийти дважды за один прогон: по живому SSE (_seq) и из
// /api/report (pollReport после обрыва/F5 — без _seq, дедуп по seq не
// ловит). Показываем итог один раз; «прогон»-разделитель сбрасывает.
let runDoneShown = false;

function renderEvent(ev: AgentEvent): void {
  const s = ev._seq ?? 0;
  if (s && s <= lastSeq) return;   // уже показано (SSE или поллер)
  if (s) lastSeq = s;
  // done закрывает ожидание итога: persist() ниже сохранит флаг, и
  // restore() после F5 не станет опрашивать завершённый прогон.
  if (ev.phase === "done") awaitingReport = false;
  applyEvent(state, ev);
  renderPanel();
  persist();
  switch (ev.phase) {
    case "start": {
      // Разделитель между прогонами — лента читается как серия
      // «сессий», а не бесконечный поток.
      runDoneShown = false;
      const sep = document.createElement("div");
      sep.className = "msg msg--runsep";
      sep.innerHTML = "<span>прогон</span>";
      chat.append(sep);
      // Прогон из другого клиента (curl/вкладка): показываем его
      // задачу бабблом пользователя — как будто её ввели тут.
      if (ev.task && ev.task !== lastLocalTask) bubble(esc(ev.task), "user");
      break;
    }
    case "status":
      setStatus(ev.message);
      break;
    case "progress":
      setStatus(
        `сервер: ${esc(ev.message || "прогон идёт")} · ` +
          `шаг ${ev.steps} · llm ${ev.llm_calls} · ~${ev.tokens} tok`);
      break;
    case "thinking":
      setStatus(`модель думает… (шаг ${ev.step})`);
      break;
    case "think": {
      clearStatus();
      const llmMs = ev.llm_ms
        ? ` <span class="msg__result">llm ${(ev.llm_ms / 1000).toFixed(1)}s</span>`
        : "";
      if ((ev.reasoning ?? "").length > COLLAPSE_LEN) {
        // Длинное рассуждение — в схлопнутый блок, чат не раздуваем.
        foldBubble(
          `💭 <b>#${ev.step}</b> — размышление модели${llmMs}`,
          ev.reasoning ?? "", "think");
      } else {
        bubble(`💭 <b>#${ev.step}</b> ${esc(ev.reasoning)}${llmMs}`, "think");
      }
      break;
    }
    case "llm_prompt": {
      // Промпт = такое же баббл-сообщение, как текст пользователя:
      // бейдж «→ LLM» сверху, текст промпта как тело сообщения.
      const d = document.createElement("div");
      d.className = "msg msg--prompt";
      const head = document.createElement("div");
      head.className = "prompt-head";
      head.textContent = `📤 АГЕНТ → LLM · шаг ${ev.step}` +
        (ev.url ? ` · ${decodeUrl(ev.url)}` : "");
      const pre = document.createElement("pre");
      pre.textContent = ev.prompt;
      pre.hidden = true;
      head.addEventListener("click", () => {
        pre.hidden = !pre.hidden;
        head.classList.toggle("prompt-head--open", !pre.hidden);
      });
      d.append(head, pre);
      chat.append(d);
      chat.scrollTop = chat.scrollHeight;
      break;
    }
    case "scenario": {
      // Сценарий/политика прогона — постоянный баббл (раньше шли
      // транзиентным status и затирались первым же think/snapshot).
      clearStatus();
      const deny = ev.deny_tools?.length
        ? ` · deny: ${esc(ev.deny_tools.join(", "))}` : "";
      bubble(
        `сценарий: <b>«${esc(ev.name)}»</b> ` +
        `<span class="msg__result">${esc(ev.source)}: ` +
        `${esc(ev.url)}</span><br>` +
        `политика: ${esc(ev.policy)}` +
        `${ev.read_only ? " · read_only" : ""}${deny}` +
        ` · scope: ${esc(ev.domains?.join(", ") || "*")}`);
      break;
    }
    case "classify":
      clearStatus();
      bubble(
        `классификация: <b>${esc(ev.verdict)}</b> ${esc(ev.reason ?? "")}`);
      break;
    case "snapshot":
      if (ev.url && !targetUrl) {
        targetInput.value = ev.url;   // показать, куда поехал прогон
      }
      clearStatus();
      if (!ev.silent) {
        bubble(
          `снапшот: <b>${esc(ev.title || decodeUrl(ev.url ?? "") || "")}</b> — ` +
            `${ev.elements} элементов`);
      }
      break;
    case "confirm": {
      // Human-in-the-loop: агент встал на паузу и ждёт Approve/Deny.
      // Модалка показывает ТОЧНЫЙ вызов — человек решает по tool+args.
      clearStatus();
      const c = ev.call;
      confirmationId = ev.request_id;
      confirmText.textContent = c.plan
        ? `Переместить в корзину ${c.plan.targets.length} файлов из указанной папки? Только загруженные строки. Без ответа за ${ev.timeout_seconds ?? 90} с — отмена и остановка.`
        : `Политика требует вашего решения (${ev.reason ?? "policy"}). Без ответа — отмена. Страница: ${ev.snapshot_url ? decodeUrl(ev.snapshot_url) : "?"}`;
      confirmCall.textContent = c.plan
        ? `Папка: ${c.plan.folder}\n\n${c.plan.targets.map((f, i) => `${i + 1}. ${f.name}\n   ID: ${f.id}`).join("\n")}\n\nРазрешение действует только на этот список, один раз.`
        : (c.element_name ? `Цель: «${c.element_name}»\n\n` : "") +
          `${c.tool}(${JSON.stringify(c.args ?? {}, null, 1)})`;
      confirmModal.hidden = false;
      confirmNo.focus();
      bubble(`⏸ прогон на паузе — ждёт вашего решения: ` +
             `<b>${esc(c.tool)}</b>(${esc(JSON.stringify(c.args ?? {}))})`,
             "status");
      break;
    }
    case "confirm_result": {
      confirmModal.hidden = true;
      bubble(ev.approved
        ? `✅ вы разрешили: ${esc(ev.call.tool)}`
        : ev.auto
          ? `🚫 автоматический отказ: ${esc(ev.call.tool)} — ` +
            `${esc(ev.auto)}`
          : `🚫 вы запретили: ${esc(ev.call.tool)} — ` +
            `модель получит отказ и перепланирует`, "report");
      break;
    }
    case "step": {
      clearStatus();
      const d = ev.decision;
      // Вердикт шага — цветной бейдж, а не голый текст: сканировать
      // ленту по цвету проще (ok=зелёный, blocked/invalid/verify=красный).
      const mark = ev.blocked
        ? `<span class="badge badge--bad">⛔ ${esc(ev.blocked)}</span>`
        : ev.error
          ? `<span class="badge badge--bad">invalid: ${esc(ev.error)}</span>`
          // skipped — вызов не исполнялся (детектор петли): нейтральный
          // бейдж, не зелёный ✓ — иначе читается как успешный клик.
          : ev.skipped
            ? `<span class="badge">≡ пропущен — повтор</span>`
            : ev.verified === false
              ? `<span class="badge badge--bad">verify ✗</span>`
              : `<span class="badge badge--ok">✓</span>`;
      const browserMs = ev.browser_ms
        ? ` <span class="msg__result">${ev.browser_ms}ms</span>` : "";
      if ((ev.result ?? "").length > COLLAPSE_LEN) {
        // Длинный результат инструмента (текст страницы и т.п.) —
        // шапка = вызов+вердикт, тело = полный результат, закрыто.
        foldBubble(
          `<b>#${ev.step}</b> <b>${esc(d.tool)}</b>(${esc(
            JSON.stringify(d.args ?? {}))}) ${mark}${browserMs}`,
          (d.reasoning ? `${d.reasoning}\n\n` : "") + (ev.result ?? ""),
          "step");
      } else {
        // Карточка шага: шапка (номер + tool-чип + args + бейдж),
        // под ней — reasoning курсивом и результат инструмента.
        const head =
          `<div class="step__head">` +
          `<span class="step__num">#${ev.step}</span>` +
          `<code class="chip">${esc(d.tool)}</code>` +
          `<code class="step__args">${esc(JSON.stringify(d.args ?? {}))}</code>` +
          `${mark}${browserMs}</div>`;
        const reason = d.reasoning
          ? `<div class="step__reason">${esc(d.reasoning)}</div>` : "";
        const result = ev.result
          ? `<div class="step__result">${esc(ev.result)}</div>` : "";
        bubble(head + reason + result, "step");
      }
      break;
    }
    case "done": {
      confirmModal.hidden = true;
      confirmationId = undefined;
      clearStatus();
      const r = ev.report;
      if (!r || runDoneShown) break;   // дубль done (SSE+pollReport)
      runDoneShown = true;
      if (r.answer) {
        bubble(answerHtml(r.answer), "answer");
      }
      bubble(
        `итог: <b>${esc(r.status)}</b> — шагов ${r.steps ?? "—"}, ` +
          `вызовов LLM ${r.llm_calls ?? "—"}, ` +
          `токенов ${r.tokens ?? "—"}` +
          // Длительность прогона — от start_ts на сервере.
          (r.elapsed_s != null ? `, ${r.elapsed_s}с` : "") +
          (r.stop_reason ? `, стоп: ${esc(r.stop_reason)}` : ""),
        "report");
      break;
    }
    case "error":
      clearStatus();
      bubble(`ошибка: ${esc(ev.message)}`, "error");
      break;
  }
}

/* --- запуск --------------------------------------------------------------- */
// Пока идёт прогон, новые сообщения не отправляются: агент владеет одним
// браузером, параллельный прогон сломал бы разделяемое состояние.
let running = false;
let runAbort: AbortController | null = null;
// Метка старта активного прогона (unix-секунды — сравниваем с rep.ts
// итога) и флаг «done ещё не было»: persist() кладёт их в storage,
// restore() после F5 по ним возобновляет опрос итога.
let runStartTs = 0;
let awaitingReport = false;

/** F5 посреди прогона: стрим умер, но прогон на бэкенде жив —
 *  возобновляем опрос /api/report и дорисовываем итог в чат. */
async function resumePolling(runStart: number): Promise<void> {
  setRunning(true);
  stopBtn.disabled = false;
  runAbort = new AbortController();
  awaitingReport = true;
  const delivered = await pollReport(runStart, renderEvent,
    runAbort.signal);
  if (!delivered) {
    awaitingReport = false;
    bubble(runAbort.signal.aborted
      ? "ожидание итога остановлено"
      : "итог с сервера не дошёл — прогон был прерван", "error");
  }
  runAbort = null;
  setRunning(false);
}

function setRunning(on: boolean): void {
  running = on;
  sendBtn.disabled = on;
  stopBtn.hidden = !on;                  // «стоп» виден только во время прогона
  // Placeholder держим в одну строку: длинный переносится и autosize()
  // раздувает пустое поле по высоте wrapped-текста.
  taskIn.placeholder = on
    ? "агент работает — «Стоп» прервёт прогон…"
    : "Ссылка + задание… (Enter — отправить, Shift+Enter — перенос)";
}

async function stopRun(): Promise<void> {
  // НЕ отсеиваем по флагу running: после обрыва SSE флаг мог остаться
  // старым, а прогон на сервере ещё жив — клик делал бы «ничего».
  stopBtn.disabled = true;
  // Серверная остановка (между шагами) + рвём наш стрим сразу,
  // не дожидаясь итогового "done".
  fetch(`${API_BASE}/api/stop`, { method: "POST" }).catch(() => {});
  confirmModal.hidden = true;      // стоп = отказ для висящего confirm
  runAbort?.abort();
  // Стрим убит до done — итог забираем поллингом, иначе UI молчит
  // и выглядит как «Стоп не сработал».
  void resumePolling(runStartTs);
}

/** Ответ на confirm-модалку: один POST — backend будит прогон.
 *  Повторный клик ничего не исполнит дважды: pending одноразовый. */
function answerConfirm(approve: boolean): void {
  confirmModal.hidden = true;
  confirmYes.disabled = confirmNo.disabled = true;
  fetch(`${API_BASE}/api/confirm`, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ approve, request_id: confirmationId }),
  }).then(async (response) => {
    if (!response.ok) throw new Error(`Подтверждение не принято: HTTP ${response.status}`);
  }).catch((error: unknown) => {
    bubble(esc(error instanceof Error ? error.message : String(error)), "error");
  }).finally(() => {
    confirmYes.disabled = confirmNo.disabled = false;
  });
}

function autosize(): void {
  if (!taskIn.value) {
    taskIn.style.height = "";       // rows=1 — родная высота
    return;
  }
  taskIn.style.height = "auto";
  taskIn.style.height = `${taskIn.scrollHeight}px`;
}

async function run(): Promise<void> {
  const task = taskIn.value.trim();
  if (!task || running) return;

  // Страницу задаёт ТОЛЬКО «поле наверху»: ссылка в тексте задачи
  // scope не меняет (иначе «зайди на x.com» переключало бы сайт мимо
  // поля — и мимо видимой политики). Единственное исключение — поле
  // пустое: ссылка из первого сообщения работает бутстрапом.
  const found = task.match(URL_RE);
  if (found && !targetUrl) {
    setTarget(found[0].replace(/[.,;:!?]+$/, ""));
  } else if (!targetUrl) {
    bubble(
      "сначала задайте страницу в поле наверху, напр.: " +
        "<code>https://quotes.toscrape.com</code>");
    return;
  }

  lastLocalTask = task;
  bubble(esc(task), "user");
  taskIn.value = "";                       // поле очищается после отправки
  autosize();
  // Снапшот/инструменты переживают сброс прогона — новый run на той же
  // странице перезаполнит их первым snapshot-событием, а до него
  // лучше показать известное состояние, чем «—».
  const snap = state.snapshot, toolList = state.tools;
  state = initialState();
  state.snapshot = snap;
  state.tools = toolList;
  renderPanel();
  setRunning(true);
  stopBtn.disabled = false;
  runAbort = new AbortController();
  // persist() сразу: F5 в окно до первого SSE-события тоже восстановится.
  runStartTs = Date.now() / 1000;
  awaitingReport = true;
  persist();
  try {
    await streamRun(
      // target = «поле наверху»: follow-up без ссылки идёт на ту же
      // страницу, а бэкенд подцепит контекст прошлой сессии.
      { scenario: "auto", llm: LLM, task,
        headed: HEADED, target: targetUrl },
      renderEvent,
      runAbort.signal,
    );
  } catch (e) {
    if (!runAbort.signal.aborted) {
      clearStatus();
      // До бэкенда не дошли — итога не будет, флаг ожидания снимаем,
      // иначе после F5 опрос /api/report ждал бы его впустую.
      awaitingReport = false;
      bubble(`запрос прерван: ${esc(e)}`, "error");
    }
  }
  runAbort = null;
  setRunning(false);
}

// «Разведать»: read-only прогон поверх выбранной страницы — снапшот и
// черновик политики от модели (этап разведки лабораторной).
async function analyze(): Promise<void> {
  if (running) return;
  if (!targetUrl) {
    bubble("сначала задайте страницу в поле наверху");
    return;
  }
  bubble(`разведка страницы: ${esc(targetUrl)}`, "user");
  const snap = state.snapshot, toolList = state.tools;
  state = initialState();
  state.snapshot = snap;
  state.tools = toolList;
  renderPanel();
  setRunning(true);
  stopBtn.disabled = false;
  runAbort = new AbortController();
  runStartTs = Date.now() / 1000;
  awaitingReport = true;
  persist();
  try {
    await streamRun(
      { scenario: "auto", llm: LLM, task: "", headed: HEADED,
        target: targetUrl, analyze: true },
      renderEvent,
      runAbort.signal,
    );
  } catch (e) {
    if (!runAbort.signal.aborted) {
      clearStatus();
      awaitingReport = false;
      bubble(`запрос прерван: ${esc(e)}`, "error");
    }
  }
  runAbort = null;
  setRunning(false);
}

// «Новый чат»: полный сброс — чат, панель, целевая страница.
// Во время прогона не сбрасываем (для этого «Стоп»).
clearBtn.addEventListener("click", () => {
  if (running) return;
  sessionStorage.removeItem(CHAT_KEY);
  sessionStorage.removeItem(STATE_KEY);
  sessionStorage.removeItem(PAGEHIDE_KEY);
  // lastSeq НЕ трогаем: серверный SEQ монотонный — события нового
  // прогона придут с бо́льшими номерами и не отфильтруются поллером.
  chat.innerHTML = "";
  statusEl = null;
  state = initialState();
  setTarget("");                    // чистит ba-target-url и поле наверху
  renderPanel();
  // Серверный сброс обязателен: буфер EVENTS реплеил бы старый прогон
  // при F5, а session.json вернул бы контекст «продолжи». Без ответа
  // бэкенда локальный сброс уже сделан — просто логируем.
  fetch("/api/clear", { method: "POST" })
    .catch((e) => console.warn("серверный сброс не удался:", e));
  bubble("новый чат — задайте страницу и задачу");
});

// «Диаграмма пути прогона»: рендерим mermaid по клику и открываем
// готовый SVG картинкой в новой вкладке (blob-URL). dynamic import
// держит mermaid (~1МБ) вне стартового бандла.
$("diagramLink").addEventListener("click", async (e) => {
  e.preventDefault();
  try {
    const { default: mermaid } = await import("mermaid");
    const css = getComputedStyle(document.documentElement);
    const tok = (n: string) => css.getPropertyValue(n).trim();
    mermaid.initialize({
      startOnLoad: false,
      theme: "base",
      // htmlLabels:false — подписи как чистый SVG <text>, без
      // foreignObject/XHTML: mermaid пишет <br> без закрытия, что
      // для image/svg+xml в новой вкладке — XML parse error.
      flowchart: { htmlLabels: false },
      themeVariables: {
        background: tok("--color-bg"),
        primaryColor: tok("--color-panel"),
        primaryTextColor: tok("--color-fg"),
        primaryBorderColor: tok("--color-line"),
        lineColor: tok("--color-dim"),
        secondaryColor: tok("--color-user"),
        tertiaryColor: tok("--color-panel"),
        fontFamily: "system-ui, sans-serif",
      },
    });
    const { svg } = await mermaid.render(
      "labDiagram", $("diagramSrc").textContent ?? "");
    // Страховка под XML-парсер image-документа: любые оставшиеся
    // <br> (XHTML-лейблы) закрываем — иначе «mismatched tag».
    const svgXml = svg.replace(/<br>/g, "<br/>");
    // Фоновый rect первым элементом: в новой вкладке svg лежит на
    // дефолтном БЕЛОМ фоне браузера — тёмная тема была бы нечитаема.
    // Если mermaid уже поставил свой фон — наш подложит под него
    // тем же цветом, вреда нет.
    const img = svgXml.replace(/(<svg[^>]*>)/,
      `$1<rect width="100%" height="100%" fill="${tok("--color-bg")}"/>`);
    const url = URL.createObjectURL(
      new Blob([img], { type: "image/svg+xml" }));
    window.open(url, "_blank", "noopener");
    // revoke с задержкой: вкладка должна успеть прочитать blob.
    setTimeout(() => URL.revokeObjectURL(url), 60_000);
  } catch (err) {
    bubble(`диаграмма не отрисовалась: ${esc(String(err))}`, "error");
  }
});
sendBtn.addEventListener("click", run);
analyzeBtn.addEventListener("click", analyze);
stopBtn.addEventListener("click", stopRun);
$("togglePanel").addEventListener("click", () => {
  document.querySelector(".lab")?.classList.toggle("lab--panel-open");
});
confirmYes.addEventListener("click", () => answerConfirm(true));
confirmNo.addEventListener("click", () => answerConfirm(false));
document.addEventListener("keydown", (e) => {
  // Esc в модалке = Запретить (безопасный дефолт).
  if (e.key === "Escape" && !confirmModal.hidden) answerConfirm(false);
});
taskIn.addEventListener("keydown", (e) => {
  // Enter — отправить, Shift+Enter — новая строка (как в чатах).
  if (e.key === "Enter" && !e.shiftKey) {
    e.preventDefault();
    run();
  }
});
taskIn.addEventListener("input", autosize);
renderTarget();
// Сначала boot-метка бэкенда: если процесс перезапускался, чат в
// sessionStorage — фантом старого прогона; чистим ДО restore(), иначе
// он нарисует старую ленту и поллер сотрёт её только через 2.5 с.
void (async () => {
  try {
    const r = await fetch(`/api/events?after=${lastSeq}`);
    const j = await r.json() as { events?: AgentEvent[] };
    for (const ev of j.events || []) renderEvent(ev);
  } catch { /* backend оффлайн — показываем, что сохранилось */ }
  restore();
  renderPanel();
})();
