import { defineConfig } from "vite";

// Последний рубеж против падения dev-сервера: когда браузер рвёт
// SSE-стрим /api/run посреди прогона, запись в мёртвый сокет даёт
// EPIPE/ECONNRESET — ошибка всплывает на сокете БЕЗ обработчика
// (http-proxy не всегда маршрутизирует её в proxy "error"), и node
// умирает на unhandled. Глотаем только сетевые коды — остальное падает
// как обычно, чтобы реальные баги не маскировать.
const IGNORABLE = new Set(["EPIPE", "ECONNRESET", "ERR_STREAM_DESTROYED"]);
process.on("uncaughtException", (err: NodeJS.ErrnoException) => {
  if (IGNORABLE.has(err.code ?? "")) {
    console.warn("[dev] client socket closed:", err.code);
    return;
  }
  throw err;
});

// UI живёт на vite dev-сервере (:5173) — serve.py (:8090) только API,
// /api проксируется сюда. `npm run build` кладёт артефакт в dist/ —
// для демо он не нужен.
export default defineConfig({
  server: {
    port: 5173,
    proxy: {
      "/api": {
        target: "http://localhost:8090",
        // SSE-стрим рвётся, когда клиент закрывает страницу/стопит прогон:
        // без обработчика node умирает на unhandled EPIPE и весь dev-
        // сервер падает. Глотаем — это нормальное закрытие соединения.
        configure: (proxy) => {
          proxy.on("error", (err, _req, res) => {
            console.warn("[proxy] client/ closed:", (err as NodeJS.ErrnoException).code ?? err);
            if (res && !res.headersSent) res.writeHead(502).end();
            else res?.end();
          });
          // EPIPE прилетает и на сырые сокеты минуя proxy "error"
          // (запись в уже закрытый браузером сокет) — без обработчика
          // node падает на unhandled 'error' event. Покрываем обе
          // стороны: сокет клиента и upstream-сокет proxyReq → :8090.
          proxy.on("proxyReq", (pr, req, res) => {
            pr.on("error", () => {});
            pr.on("socket", (s) => s.on("error", () => {}));
            req.socket?.on("error", () => {});
            res.socket?.on("error", () => {});
          });
        },
      },
    },
  },
});
