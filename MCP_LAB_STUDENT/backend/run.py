"""run.py — CLI: прогнать агента на реальном сайте (headless-проверка).

    python run.py --url https://en.wikipedia.org \
        --task 'найди статью про Alan Turing и назови первое предложение'
    python run.py --task 'https://quotes.toscrape.com назови первую цитату'
    python run.py --analyze --url https://books.toscrape.com

    Модель — локальная Ollama (OLLAMA_MODEL, default qwen3:4b).
"""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent
sys.path.insert(0, str(ROOT))

from agent import analyze_site, run_agent        # noqa: E402
from llm import get_llm                          # noqa: E402
from scenarios import extract_url, scenario_for_url  # noqa: E402


def load_env() -> None:
    """Подхватить KEY=VALUE из .env рядом с проектом (без сторонних пакетов)."""
    env_file = ROOT / ".env"
    if not env_file.exists():
        return
    import os
    for line in env_file.read_text().splitlines():
        line = line.strip()
        if line and not line.startswith("#") and "=" in line:
            k, v = line.split("=", 1)
            os.environ.setdefault(k.strip(),
                                  v.split("#", 1)[0].strip())


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--task", default=None,
                    help="команда агенту")
    ap.add_argument("--url", default=None,
                    help="стартовая страница; scope ограничится её доменом")
    ap.add_argument("--headed", action="store_true",
                    help="видимое окно Chrome (или HEADED=1)")
    ap.add_argument("--cdp", default=None, metavar="URL",
                    help="CDP endpoint живого Chrome "
                         "(default http://localhost:9222; --no-cdp "
                         "выключает)")
    ap.add_argument("--no-cdp", action="store_true",
                    help="чистый браузер без CDP (без ваших логинов)")
    ap.add_argument("--analyze", action="store_true",
                    help="этап A: read-only разведка страницы → черновик "
                         "политики сценария (нужна ссылка в --task/--url)")
    args = ap.parse_args()

    load_env()
    import os
    # CDP по умолчанию: живой Chrome с вашими логинами (профиль
    # /tmp/agent-chrome). --no-cdp — чистый браузер.
    cdp = ("" if args.no_cdp
           else args.cdp
           or os.environ.get("CDP_ENDPOINT", "http://localhost:9222"))
    task = args.task or ""
    # Единый динамический сценарий: страница из --url или из ссылки в
    # задаче; scope = её домен (read_only — см. scenarios.py, этап C).
    url = args.url or extract_url(task)
    if not url:
        ap.error("нужна страница: --url https://… или ссылка в --task")
    scenario = scenario_for_url(url)
    if not task:
        task = "Open the page and describe what you see"
    llm = get_llm()

    if args.analyze:
        print(json.dumps(analyze_site(url, llm, headed=args.headed,
                                      cdp=cdp),
                         ensure_ascii=False, indent=2))
        return 0

    def _cli_confirm(call: dict) -> bool:
        """Human-in-the-loop в терминале: показать вызов и спросить.
        Пустой ответ/Ctrl+C/EOF = отказ (fail-closed)."""
        try:
            ans = input(f"\n⚠ Агент запрашивает подтверждение: "
                        f"{call.get('tool')}({call.get('args')}) "
                        f"[y/N] ")
            return ans.strip().lower() == "y"
        except (EOFError, KeyboardInterrupt):
            return False

    rep = run_agent(task, llm, scenario, headed=args.headed,
                    cdp=cdp, confirm_fn=_cli_confirm)
    print(json.dumps(vars(rep), ensure_ascii=False, indent=2))
    return 0 if rep.status in ("done", "rejected") else 1


if __name__ == "__main__":
    sys.exit(main())
