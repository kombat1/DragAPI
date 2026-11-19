"""Файл настроек сервера платформы: backend/dragapi.toml (или путь из DRAGAPI_CONFIG / --config).

    auth = false        # без входа — только пока в платформе нет пользователей
    host = "127.0.0.1"
    port = 8765
    local_agent = true  # агент автоматизации на этой машине (нужен графический сеанс X11)
    tls_self_signed = true / tls_cert = "…" + tls_key = "…" / behind_proxy = true   # HTTPS, см. app/tls.py

Приоритет: флаг командной строки > переменная окружения > этот файл > значение по умолчанию.
"""

from __future__ import annotations

import os
import tomllib
from pathlib import Path

DEFAULT_PATH = Path(__file__).resolve().parent.parent / "dragapi.toml"
KNOWN = {"auth": bool, "host": str, "port": int, "local_agent": bool,
         # HTTPS и работа на сервере (см. app/tls.py и deploy/)
         "tls_cert": str, "tls_key": str, "tls_self_signed": bool, "tls_names": list,
         "behind_proxy": bool, "trusted_proxies": str}


class ConfigError(Exception):
    pass


def path() -> Path:
    return Path(os.environ.get("DRAGAPI_CONFIG") or DEFAULT_PATH)


def load() -> dict:
    p = path()
    try:
        mtime = p.stat().st_mtime_ns
    except FileNotFoundError:
        mtime = None
    key = (str(p), mtime)
    if _cache.get("key") != key:
        _cache.update(key=key, value=_read(p))
    return _cache["value"]


_cache: dict = {}


def _read(p: Path) -> dict:
    try:
        raw = tomllib.loads(p.read_text("utf-8"))
    except FileNotFoundError:
        if os.environ.get("DRAGAPI_CONFIG"):
            raise ConfigError(f"файл настроек {p} не найден") from None
        return {}
    except (OSError, tomllib.TOMLDecodeError) as e:
        raise ConfigError(f"не удалось прочитать {p}: {e}") from None
    for key, value in raw.items():
        if key not in KNOWN:
            raise ConfigError(f"{p}: неизвестный параметр {key!r} (допустимы: {', '.join(KNOWN)})")
        if not isinstance(value, KNOWN[key]) or (KNOWN[key] is int and isinstance(value, bool)):
            raise ConfigError(f"{p}: параметр {key!r} должен быть {KNOWN[key].__name__}")
        if KNOWN[key] is list and not all(isinstance(v, str) for v in value):
            raise ConfigError(f"{p}: параметр {key!r} — список строк")
    return raw
