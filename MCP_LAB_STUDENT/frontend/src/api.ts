/** api.ts — типы событий агента и SSE-клиент к Python-бэкенду. */

/** Прямой адрес бэкенда: SSE идёт браузер → serve.py БЕЗ vite-прокси.
 *  Прокси ронял стрим (и dev-сервер) на EPIPE, когда вкладка закрывалась
 *  посреди прогона; CORS на serve.py для прямого доступа включён. */
export const API_BASE = "http://localhost:8090";

export interface ToolCall {
  tool: string;
  args?: Record<string, unknown>;
  reasoning?: string;
  element_name?: string;  // имя элемента из снапшота для click/type — человеку видна ЦЕЛЬ, не ref
  plan?: { folder: string; coverage: string; targets: Array<{ id: string; name: string }> };
}

export interface Snapshot {
  url: string;
  title: string;
  elements: Array<{
    ref: string;
    role: string;
    name?: string;
    href?: string;
    input_type?: string;
    value?: string;
    group?: string;      // имя радио-группы (font-size и т.п.)
    checked?: boolean;   // radio/checkbox: активный вариант
  }>;
  main_text?: string;
  lead_text?: string;
}

export interface RunReport {
  status: string;
  answer?: string;            // отсутствуют при досрочной ошибке
  steps?: number;             // прогона (report={"status":"error"})
  llm_calls?: number;
  tokens?: number;
  verify_failures?: number;
  scope_violations?: number;
  stop_reason?: string;
  elapsed_s?: number;          // длительность прогона (start_ts → done)
  log_file?: string;
  errors?: string[];
}

/** События, которые шлёт agent.py/serve.py по SSE. */
export type AgentEvent = { _seq?: number } & (
  | { phase: "start"; task: string }
  | { phase: "status"; message: string }
  | { phase: "scenario"; name: string; policy: string; url: string;
      source: string; domains: string[]; read_only: boolean;
      deny_tools: string[] }
  | { phase: "tools"; tools: string[] }
  | { phase: "classify"; verdict: string; reason?: string }
  | { phase: "snapshot"; url?: string; title?: string;
      elements: number; state?: Snapshot; silent?: boolean }
  | { phase: "thinking"; step: number }
  | { phase: "think"; step: number; reasoning: string; llm_ms?: number }
  | { phase: "llm_prompt"; step: number; prompt: string; url?: string }
  | { phase: "step"; step: number; decision: ToolCall; result?: string;
      url?: string; verified?: boolean; blocked?: string; error?: string;
      skipped?: boolean; browser_ms?: number }
  | { phase: "confirm"; step: number; call: ToolCall; reason?: string;
      snapshot_url?: string; request_id?: string; timeout_seconds?: number }
  | { phase: "confirm_result"; step: number; call: ToolCall;
      approved: boolean; auto?: string }
  | { phase: "done"; report: RunReport }
  | { phase: "progress"; steps: number; llm_calls: number;
      tokens: number; message?: string }
  | { phase: "error"; message: string });



/** Потоковый POST /api/run: разбирает SSE-кадры "data: {...}\n\n".
 *  target — «поле наверху»: целевая страница для follow-up сообщений
 *  без ссылки. signal — AbortSignal кнопки «Стоп». */
export async function streamRun(
  body: { scenario: string; llm: string; task: string; headed: boolean;
          target?: string; analyze?: boolean },
  onEvent: (ev: AgentEvent) => void,
  signal?: AbortSignal,
): Promise<void> {
  const t0 = Date.now() / 1000; // метка старта — сверяем с ts итога
  const res = await fetch(`${API_BASE}/api/run`, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify(body),
    signal: signal ?? null,
  });
  if (!res.ok || !res.body) {
    // 409 «прогон уже идёт» и др.: в теле {error} — показываем его,
    // а не голый код, иначе отказ выглядит как обрыв без причины.
    let why = "";
    try {
      const j = await res.json() as { error?: string };
      why = j.error ?? "";
    } catch { /* тело не JSON — остаёмся на коде */ }
    onEvent({ phase: "error",
              message: `HTTP ${res.status}${why ? ` — ${why}` : ""}` });
    return;
  }
  const reader = res.body.getReader();
  const dec = new TextDecoder();
  let buf = "";
  // Диагностика обрывов: фаза последнего полученного события и длина
  // тишины до разрыва отличают «браузер убил fetch» (перезагрузка/HMR
  // вкладки, тишина произвольная) от «закрыт сервером» (после done).
  let lastPhase = "—";
  let lastEventAt = Date.now();
  // try — ТОЛЬКО на reader.read(): исключения JSON.parse/onEvent должны
  // падать как баги рендера, а не маскироваться под сетевой обрыв.
  let readErr: unknown = null;
  for (;;) {
    let chunk: ReadableStreamReadResult<Uint8Array>;
    try {
      chunk = await reader.read();
    } catch (e) {
      readErr = e;
      break;
    }
    if (chunk.done) break;
    buf += dec.decode(chunk.value, { stream: true });
    let i: number;
    while ((i = buf.indexOf("\n\n")) >= 0) {
      const frame = buf.slice(0, i);
      buf = buf.slice(i + 2);
      const line = frame.split("\n").find((l) => l.startsWith("data: "));
      if (line) {
        const ev = JSON.parse(line.slice(6)) as AgentEvent;
        lastPhase = ev.phase;
        lastEventAt = Date.now();
        onEvent(ev);
      }
    }
  }
  if (readErr) {
    // Свой abort — это «стоп» от пользователя, не сетевая ошибка.
    if (signal?.aborted) {
      onEvent({ phase: "status", message: "прогон остановлен" });
      return;
    }
    // Обрыв потока (сервер закрыл соединение / сеть) — показываем как
    // системное сообщение, а не как фейл рендера. Полный контекст — в
    // консоль: visibilityState=hidden указывает на уход/перезагрузку
    // вкладки как источник разрыва, а не на сервер; e.message отличает
    // сетевой TypeError от бага, залетевшего сюда по ошибке.
    const err = readErr instanceof Error ? readErr : null;
    console.warn("[api] SSE-стрим оборвался", {
      errName: err?.name, errMessage: err?.message, errStack: err?.stack,
      err: readErr, lastPhase,
      idleMs: Date.now() - lastEventAt,
      elapsedMs: Date.now() - t0 * 1000,
      visibility: document.visibilityState,
    });
    onEvent({ phase: "error", message:
      `соединение оборвалось: ${readErr} — last: ${lastPhase}, ` +
      `тишина ${((Date.now() - lastEventAt) / 1000).toFixed(0)}с` });
    // Прогон на бэкенде мог продолжиться и дойти до конца — дочитываем
    // итог через /api/report, иначе сгенерированный ответ терялся.
    onEvent({ phase: "status", message: "жду итог с сервера…" });
    await pollReport(t0, onEvent, signal);
  }
}

/** Опрос /api/report после обрыва стрима ИЛИ перезагрузки вкладки:
 *  прогон на бэкенде доживает без SSE — дочитываем его итог.
 *  Принимаем только итог, записанный ПОСЛЕ sinceTs (unix-секунды, как
 *  rep.ts на бэкенде) — иначе показали бы ответ прошлого прогона.
 *  Возвращает true, если итог доехал и ушёл в onEvent. */
export async function pollReport(
  sinceTs: number,
  onEvent: (ev: AgentEvent) => void,
  signal?: AbortSignal,
): Promise<boolean> {
  const deadline = Date.now() + 6 * 60_000;   // прогон длинный (CPU-LLM)
  while (Date.now() < deadline && !signal?.aborted) {
    await new Promise((r) => setTimeout(r, 4000));
    // Живой прогресс прогона: /api/status отдаёт счётчики и статус,
    // пока итога ещё нет — ожидание выглядит работой, а не зависанием.
    // fetch/parse — в try; onEvent — СНАРУЖИ: исключение рендера на
    // progress/done не должно маскироваться под «сервер недоступен» и
    // крутить опрос вечно (та же ловушка, что у readErr выше).
    let st: { running?: boolean; steps?: number; llm_calls?: number;
              tokens?: number; status?: string } | null = null;
    try {
      st = await fetch(`${API_BASE}/api/status`).then((r) => r.json());
    } catch (e) {
      console.debug("[api] /api/status недоступен", e);
    }
    if (st?.running) {
      onEvent({ phase: "progress", steps: st.steps ?? 0,
                llm_calls: st.llm_calls ?? 0,
                tokens: st.tokens ?? 0,
                message: st.status ?? "" });
    }
    let rep: { report?: RunReport; ts?: number } | null = null;
    try {
      rep = await fetch(`${API_BASE}/api/report`).then((r) => r.json());
    } catch (e) {
      // Сервер недоступен — следующая попытка; лог в консоль, чтобы
      // опрос не выглядел мёртвым зависанием.
      console.debug("[api] /api/report недоступен", e);
    }
    if (rep?.report && typeof rep.ts === "number" &&
        rep.ts >= sinceTs - 5) {
      onEvent({ phase: "done", report: rep.report });
      return true;
    }
    if (st && st.running === false) {
      // Сервер отвечает и прогона НЕТ: итога уже не будет — run
      // умер вместе с процессом (рестарт serve.py) или done ушёл
      // до sinceTs. CURR.running=False и LAST.report ставятся в
      // одном синхронном блоке, так что «running=false без свежего
      // отчёта» — надёжный сигнал смерти прогона: выходим сразу,
      // а не ждём 6 минут впустую.
      onEvent({ phase: "status", message:
        "прогон завершился без итога — бэкенд был перезапущен" });
      return false;
    }
  }
  return false;
}
