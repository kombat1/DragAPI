"""Различия Linux / macOS / Windows в одном месте: пути в виртуальных окружениях, интерпретатор
для песочницы, минимальное окружение дочерних процессов, лимиты ресурсов."""

from __future__ import annotations

import os
import socket
import sys
from pathlib import Path

IS_WINDOWS = sys.platform == "win32"
IS_MAC = sys.platform == "darwin"
IS_LINUX = sys.platform.startswith("linux")
# unix-сокеты: на Windows — локальный TCP-порт
UNIX_SOCKETS = hasattr(socket, "AF_UNIX") and not IS_WINDOWS


def venv_python(venv: str | Path) -> Path:
    """Интерпретатор виртуального окружения: bin/python или Scripts\\python.exe."""
    venv = Path(venv)
    return venv / "Scripts" / "python.exe" if IS_WINDOWS else venv / "bin" / "python"


def site_packages(venv: str | Path) -> Path | None:
    venv = Path(venv)
    if IS_WINDOWS:
        p = venv / "Lib" / "site-packages"
        return p if p.exists() else None
    return next(venv.glob("lib/python*/site-packages"), None)


def system_python() -> str:
    """Интерпретатор для песочницы и окружений проектов. На Linux — системный (firejail видит его
    по стандартному пути), на macOS и Windows — тот же, на котором работает платформа."""
    if IS_LINUX and os.path.exists("/usr/bin/python3"):
        return "/usr/bin/python3"
    return sys.executable


def clean_env(**extra: str) -> dict[str, str]:
    """Почти пустое окружение для чужого кода: без секретов и настроек платформы. На Windows без
    SYSTEMROOT и TEMP не работают сокеты и временные файлы — их оставляем."""
    env = {"LC_ALL": "C.UTF-8", "PYTHONIOENCODING": "utf-8", "OPENBLAS_NUM_THREADS": "1",
           "OMP_NUM_THREADS": "1", "MKL_NUM_THREADS": "1"}
    if IS_WINDOWS:
        for key in ("SYSTEMROOT", "SYSTEMDRIVE", "WINDIR", "TEMP", "TMP", "PATHEXT", "COMSPEC"):
            if os.environ.get(key):
                env[key] = os.environ[key]
        env.pop("LC_ALL")
        env["PYTHONUTF8"] = "1"
    elif IS_MAC:
        env["LC_ALL"] = "en_US.UTF-8"
        env["PATH"] = "/usr/bin:/bin:/usr/sbin:/sbin"
    else:
        env["PATH"] = "/usr/bin:/bin"
    env.update({k: v for k, v in extra.items() if v is not None})
    return env


def set_limits(limits: dict[str, tuple[int, int]]) -> None:
    """setrlimit там, где он есть (Linux, macOS). Неподдерживаемые лимиты (RLIMIT_AS на macOS)
    пропускаем: остаётся лимит времени."""
    try:
        import resource
    except ImportError:          # Windows
        return
    for name, value in limits.items():
        res = getattr(resource, name, None)
        if res is None:
            continue
        try:
            resource.setrlimit(res, value)
        except (ValueError, OSError):
            pass


def limits_supported() -> bool:
    return not IS_WINDOWS
