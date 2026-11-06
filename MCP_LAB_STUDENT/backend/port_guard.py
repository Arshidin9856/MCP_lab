"""port_guard.py — авто-рестарт: освободить TCP-порт от ПРЕЖНЕГО
экземпляра того же сервиса, чтобы не было 'Address already in use'.

Используется serve.py (API :8090) и tools/serve_static.py
(attack_pages :8777).

Безопасность: убиваем ТОЛЬКО процессы, чья командная строка содержит
одну из подстрок `owns` — чужой слушатель порта не трогаем, выходим
с понятной ошибкой.
"""
from __future__ import annotations

import os
import signal
import subprocess
import sys
import time
from pathlib import Path


def _listener_pids_proc(port: int) -> list[int]:
    """PID'ы LISTEN-сокетов на порту — Linux, чистый /proc, без lsof/ss.

    /proc/net/tcp{,6} → inode сокета в состоянии 0A (LISTEN) →
    /proc/<pid>/fd → чей symlink 'socket:[inode]'."""
    inodes: set[str] = set()
    for table in ("/proc/net/tcp", "/proc/net/tcp6"):
        try:
            lines = Path(table).read_text().splitlines()[1:]
        except OSError:
            continue
        for ln in lines:
            parts = ln.split()
            if len(parts) > 9 and parts[3] == "0A" and \
                    int(parts[1].rsplit(":", 1)[1], 16) == port:
                inodes.add(parts[9])
    if not inodes:
        return []
    pids: list[int] = []
    for proc in Path("/proc").iterdir():
        if not proc.name.isdigit():
            continue
        try:
            for fd in (proc / "fd").iterdir():
                try:
                    link = os.readlink(fd)
                except OSError:
                    continue        # fd закрылся между iterdir и readlink
                if link.startswith("socket:[") and link[8:-1] in inodes:
                    pids.append(int(proc.name))
                    break
        except OSError:
            continue                # процесс умер или чужой — пропускаем
    return pids


def _listener_pids_win(port: int) -> list[int]:
    """То же для Windows: `netstat -ano -p tcp`, строки LISTENING."""
    try:
        out = subprocess.run(["netstat", "-ano", "-p", "tcp"],
                             capture_output=True, text=True,
                             timeout=15).stdout
    except (OSError, subprocess.TimeoutExpired):
        return []
    pids: set[int] = set()
    for ln in out.splitlines():
        parts = ln.split()
        if len(parts) >= 5 and "LISTENING" in ln and \
                parts[1].rsplit(":", 1)[-1] == str(port):
            pids.add(int(parts[-1]))
    return sorted(pids)


def _listener_pids(port: int) -> list[int]:
    """Платформенная обёртка: Windows — netstat, остальное — /proc."""
    return (_listener_pids_win(port) if os.name == "nt"
            else _listener_pids_proc(port))


def _pid_cmdline(pid: int) -> str:
    """Командная строка процесса — нужна для проверки «это наш сервис».

    Без этой проверки авто-рестарт превращается в убийство случайной
    программы, севшей на порт: чужой процесс НЕ трогаем."""
    try:
        raw = Path(f"/proc/{pid}/cmdline").read_bytes()
        return raw.replace(b"\0", b" ").decode(errors="replace").strip()
    except OSError:
        pass                        # не Linux или процесс умер
    if os.name == "nt":
        try:
            out = subprocess.run(
                ["powershell", "-NoProfile", "-Command",
                 f"(Get-CimInstance Win32_Process -Filter "
                 f"'ProcessId={pid}').CommandLine"],
                capture_output=True, text=True, timeout=15).stdout
            return out.strip()
        except (OSError, subprocess.TimeoutExpired):
            return ""
    return ""


def _pid_alive(pid: int) -> bool:
    """Жив ли процесс: kill(0) без сигнала — только проверка существования."""
    try:
        os.kill(pid, 0)
        return True
    except OSError:
        return False


def _kill(pid: int) -> None:
    """SIGTERM → до 2 с на выход → SIGKILL. На Windows os.kill уже
    завершает процесс (TerminateProcess), градация мягкая."""
    for sig in (signal.SIGTERM, getattr(signal, "SIGKILL", signal.SIGTERM)):
        try:
            os.kill(pid, sig)
        except OSError:
            return                  # уже умер между проверками
        deadline = time.monotonic() + 2.0
        while time.monotonic() < deadline and _pid_alive(pid):
            time.sleep(0.1)
        if not _pid_alive(pid):
            return


def ensure_port_free(port: int, owns: tuple[str, ...],
                     tag: str = "serve") -> bool:
    """Освободить порт от ПРЕЖНЕГО экземпляра — авто-рестарт без
    'Address already in use'.

    owns — подстроки командной строки, по которым слушатель признаётся
    «своим» (напр. ("serve.py",) или ("http.server", "serve_static.py")).
    Чужой процесс на порту не трогаем: False + сообщение в stderr.
    tag — префикс диагностических строк ([tag] ...)."""
    for pid in _listener_pids(port):
        if pid == os.getpid():
            continue
        cmd = _pid_cmdline(pid)
        if not any(s in cmd for s in owns):
            print(f"[{tag}] порт {port} занят чужим процессом "
                  f"PID {pid} ({cmd or 'cmdline недоступен'}) — "
                  f"не убиваю. Освободите порт или задайте --port.",
                  file=sys.stderr, flush=True)
            return False
        print(f"[{tag}] порт {port} держит прежний экземпляр "
              f"PID {pid} — останавливаю для рестарта",
              file=sys.stderr, flush=True)
        _kill(pid)
    deadline = time.monotonic() + 5.0
    while time.monotonic() < deadline:
        if not _listener_pids(port):
            return True
        time.sleep(0.1)
    print(f"[{tag}] порт {port} не освободился за 5 с",
          file=sys.stderr, flush=True)
    return False
