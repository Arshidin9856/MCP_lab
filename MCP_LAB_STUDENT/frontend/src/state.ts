/** state.ts — состояние прогона агента для боковой панели. */
import type { AgentEvent, RunReport, Snapshot } from "./api";

export interface RunState {
  status: string;            // что агент делает сейчас (строка)
  tools: string[];           // каталог инструментов от tools/list
  usedTools: Map<string, number>;  // сколько раз вызван каждый инструмент
  blockedTools: Set<string>; // инструменты, отклонённые scope-gate
  steps: number;
  llmCalls: number;
  tokens: number;
  verifyFailures: number;
  scopeViolations: number;
  snapshot: Snapshot | null;
  report: RunReport | null;
}

/** Локальная копия каталога из backend/server.py TOOLS: он статичен и
 *  одинаков для всех страниц, поэтому показываем его сразу, не дожидаясь
 *  первого прогона. Событие "tools" в начале каждого прогона
 *  перезаписывает список свежим ответом tools/list — новые инструменты
 *  из server.py подхватываются автоматически. ("task_done" сюда не
 *  входит: его renderTools дорисовывает сам — это хостовый псевдоинструмент.) */
export const DEFAULT_TOOLS = [
  "browser_navigate",
  "browser_snapshot",
  "browser_click",
  "browser_type",
  "browser_press",
  "browser_scroll",
  "browser_back",
];

export function initialState(): RunState {
  return {
    status: "ожидание",
    tools: [...DEFAULT_TOOLS],
    usedTools: new Map(),
    blockedTools: new Set(),
    steps: 0,
    llmCalls: 0,
    tokens: 0,
    verifyFailures: 0,
    scopeViolations: 0,
    snapshot: null,
    report: null,
  };
}

/** Чистый переход состояния по событию — тестируется отдельно от DOM. */
export function applyEvent(s: RunState, ev: AgentEvent): void {
  switch (ev.phase) {
    case "start":
      s.status = "запуск агента…";
      break;
    case "status":
      s.status = ev.message;
      break;
    case "scenario":
      s.status = `сценарий «${ev.name}»`;
      break;
    case "tools":
      s.tools = ev.tools;
      break;
    case "classify":
      s.status = `классификация: ${ev.verdict}`;
      break;
    case "snapshot":
      if (ev.state) s.snapshot = ev.state;
      s.status = `снапшот: ${ev.elements} элементов`;
      break;
    case "thinking":
      s.status = `модель думает… (шаг ${ev.step})`;
      break;
    case "think":
      s.status = `план готов (шаг ${ev.step})`;
      break;
    case "step": {
      const name = ev.decision?.tool ?? "?";
      s.usedTools.set(name, (s.usedTools.get(name) ?? 0) + 1);
      if (ev.blocked) s.blockedTools.add(name);
      s.steps = Math.max(s.steps, ev.step + 1);
      s.status = ev.blocked
        ? `шаг ${ev.step}: ${name} — отклонён хостом`
        : `шаг ${ev.step}: ${name}`;
      break;
    }
    case "done":
      s.report = ev.report;
      // При досрочной ошибке отчёт содержит только status — остальные
      // поля не затираем undefined.
      s.steps = ev.report.steps ?? s.steps;
      s.llmCalls = ev.report.llm_calls ?? s.llmCalls;
      s.tokens = ev.report.tokens ?? s.tokens;
      s.verifyFailures = ev.report.verify_failures ?? s.verifyFailures;
      s.scopeViolations =
        ev.report.scope_violations ?? s.scopeViolations;
      s.status = `завершено: ${ev.report.status}`;
      break;
    case "progress":
      // Живые счётчики с бэкенда при опросе /api/status после обрыва
      // стрима — панель продолжает расти, пока итога ещё нет.
      s.steps = ev.steps;
      s.llmCalls = ev.llm_calls;
      s.tokens = ev.tokens;
      s.status = `сервер: ${ev.message || "прогон идёт"}`;
      break;
    case "error":
      s.status = `ошибка: ${ev.message}`;
      break;
  }
}
