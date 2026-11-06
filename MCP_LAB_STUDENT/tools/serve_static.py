"""serve_static.py — статическая раздача attack_pages с авто-рестартом.

    python tools/serve_static.py [--port 8777]

Заменяет `python -m http.server 8777 --directory attack_pages`:
порт занят прежним экземпляром (http.server или serve_static) —
останавливаем и занимаем, как в serve.py; чужой процесс не трогаем.
"""
from __future__ import annotations

import argparse
import functools
import http.server
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent          # MCP_LAB/
sys.path.insert(0, str(ROOT / "backend"))

from port_guard import ensure_port_free                # noqa: E402


def main() -> int:
    """Раздать attack_pages/ на :8777, освободив порт от старого себя."""
    p = argparse.ArgumentParser()
    p.add_argument("--port", type=int, default=8777)
    args = p.parse_args()

    # Свой экземпляр опознаём по 'http.server' (старый способ запуска)
    # и 'serve_static' — чужих слушателей порта не убиваем никогда.
    if not ensure_port_free(args.port,
                            owns=("http.server", "serve_static"),
                            tag="attack"):
        return 1

    handler = functools.partial(
        http.server.SimpleHTTPRequestHandler,
        directory=str(ROOT / "attack_pages"))
    httpd = http.server.ThreadingHTTPServer(("127.0.0.1", args.port),
                                            handler)
    print(f"attack_pages: http://localhost:{args.port}/index.html")
    httpd.serve_forever()
    return 0


if __name__ == "__main__":
    sys.exit(main())
