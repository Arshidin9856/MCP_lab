# Подготовка ноутбука к ЛР «LLM-агент через MCP»

> Установка → проверка → удаление. Windows и Linux.
> Домашняя подготовка до пары — сам комплект лабы выдаётся на занятии.

## 1. Что установить

| Компонент | Windows | Linux |
|-----------|---------|-------|
| Python 3.11+ | `winget install Python.Python.3.12` (или python.org) | `sudo apt install python3 python3-venv` |
| Node.js 20.19+ или 22.12+ (Vite 7) | `winget install OpenJS.NodeJS.LTS` (или nodejs.org) | `nvm install --lts` (или пакет nodejs из репозитория) |
| Google Chrome | `winget install Google.Chrome` (или google.com/chrome) | `sudo apt install google-chrome-stable` (пакет .deb с google.com/chrome) |
| Ollama | `winget install Ollama.Ollama` (или OllamaSetup.exe, ~800 МБ — нормально) | `curl -fsSL https://ollama.com/install.sh \| sh` |

Браузер нужен именно системный Chrome — Playwright подключается к
нему, `playwright install` НЕ требуется.

## 2. Ollama: какую модель брать (важно)

- `qwen3:4b` — рекомендуемая: ~2,6 ГБ скачивание, ≈5 ГБ свободной RAM.
  Уверенно держит agent-loop. Брать, если на ноутбуке 12+ ГБ RAM или
  8 ГБ с закрытыми лишними программами.
- `qwen3:1.7b` — запасной вариант для слабых машин (≈2 ГБ RAM):
  разведку и простые задачи делает, но ~1 tok/s на CPU и теряется на
  многошаговых действиях. Запуск комплекта тогда с переменной
  `OLLAMA_MODEL=qwen3:1.7b`.

```bash
ollama pull qwen3:4b        # или qwen3:1.7b на слабой машине
ollama run qwen3:4b "привет"   # ОБЯЗАТЕЛЬНАЯ проверка ДО пары:
# ответ должен прийти быстро; если заметно долго — берите 1.7b
```

Если модель не влезает в свободную RAM/VRAM, ответы замедляются
в разы (зависит от железа) — поэтому проверку скорости делаем
заранее.

## 3. Проверка установки (комплект лабы ещё не нужен)

Windows (cmd/PowerShell):

```bash
python --version          # 3.11+
node --version            # v20+
npm --version
ollama run qwen3:4b "привет"   # ответ за секунды — готово
```

Linux:

```bash
python3 --version         # 3.11+
node --version            # v20+
npm --version
ollama run qwen3:4b "привет"   # ответ за секунды — готово
```

Всё. Зависимости самой лабораторной (venv, playwright, npm install)
ставим уже на паре, когда получите комплект.

## 4. Установка комплекта на занятии

```bash
python3 -m venv backend/.venv                       # Win: py -m venv backend\.venv
backend/.venv/bin/python -m pip install playwright  # Win: backend\.venv\Scripts\python -m pip install playwright
cd frontend && npm install
```

## 5. Удаление после лабораторной

Общее (обе ОС):

- Остановить `serve.py`, vite и статический сервер атак-страниц
  (Ctrl+C в каждом терминале); выйти из venv: `deactivate`.
- Удалить папку комплекта целиком — внутри лежат `.venv` и
  `node_modules`, отдельно их чистить не нужно.
- Удалить профиль агентского Chrome (логины тестовых аккаунтов):
  Windows — `%LOCALAPPDATA%\agent-chrome`, Linux —
  `~/.cache/agent-chrome`.
- По желанию — журналы прогонов `backend/logs/` и
  `backend/session.json` (если комплект оставляете).

Удаление — опционально и только штатными средствами ОС/пакетного
менеджера: `winget uninstall …` / Параметры → Приложения (Windows),
`apt remove …` (Linux), `ollama rm qwen3:4b` и `ollama stop qwen3:4b`
для модели. **Не удаляйте вручную общие данные:** чужие модели в
`~/.ollama`, основной профиль Chrome, ваши документы — под удаление
попадают только созданные для лабы данные (папка комплекта, `.venv`,
`node_modules`, профиль `agent-chrome`, журналы) и только по вашему
выбору.
