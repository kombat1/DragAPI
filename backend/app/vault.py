"""Шифрование секретных переменных окружения проектов.

На диске секрет хранится только как value_enc (Fernet: AES-128-CBC + HMAC-SHA256), поле value пустое.
Ключ платформы: переменная окружения DRAGAPI_SECRET_KEY или файл data/secret.key (создаётся при
первом запуске с правами 600). Потеря ключа = потеря секретов: храните резервную копию.

В браузер секреты не отдаются: вместо значения — has_value. Превью, тестовые запросы и «Проверить»
получают расшифрованные значения на сервере.
"""

from __future__ import annotations

import os
from pathlib import Path

from cryptography.fernet import Fernet, InvalidToken
from .paths import DATA

KEY_PATH = DATA / "secret.key"
_fernet: Fernet | None = None


class VaultError(Exception):
    pass


def _get() -> Fernet:
    global _fernet
    if _fernet is None:
        key = os.environ.get("DRAGAPI_SECRET_KEY", "").strip()
        if not key:
            if not KEY_PATH.exists():
                KEY_PATH.parent.mkdir(parents=True, exist_ok=True)
                fd = os.open(KEY_PATH, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
                with os.fdopen(fd, "wb") as f:
                    f.write(Fernet.generate_key())
            key = KEY_PATH.read_text().strip()
        try:
            _fernet = Fernet(key.encode())
        except ValueError as e:
            raise VaultError(f"некорректный ключ шифрования: {e}") from None
    return _fernet


def reset_cache():
    """Для тестов: перечитать ключ."""
    global _fernet
    _fernet = None


def encrypt(text: str) -> str:
    return _get().encrypt(text.encode("utf-8")).decode("ascii")


def decrypt(token: str) -> str:
    try:
        return _get().decrypt(token.encode("ascii")).decode("utf-8")
    except InvalidToken:
        raise VaultError("секрет зашифрован другим ключом платформы — задайте значение заново") from None


def seal_env(env: list[dict] | None, previous: list[dict] | None = None) -> list[dict]:
    """Для записи на диск: новые значения секретов шифруются, пустые — берутся из прежней версии."""
    prev = {e.get("key"): e for e in (previous or []) if e.get("secret") and e.get("value_enc")}
    out = []
    for e in env or []:
        e = {k: v for k, v in e.items() if k != "has_value"}
        if not e.get("secret"):
            e.pop("value_enc", None)
            out.append(e)
            continue
        value = e.get("value") or ""
        if value:
            e["value_enc"] = encrypt(str(value))
        elif not e.get("value_enc") and e.get("key") in prev:
            e["value_enc"] = prev[e["key"]]["value_enc"]
        e["value"] = ""
        out.append(e)
    return out


def needs_sealing(env: list[dict] | None) -> bool:
    return any(e.get("secret") and e.get("value") for e in env or [])


def mask_env(env: list[dict] | None) -> list[dict]:
    """Для браузера: без значений секретов."""
    out = []
    for e in env or []:
        e = dict(e)
        if e.get("secret"):
            e["has_value"] = bool(e.pop("value_enc", None) or e.get("value"))
            e["value"] = ""
        out.append(e)
    return out


def unseal_env(env: list[dict] | None, stored: list[dict] | None = None) -> list[dict]:
    """Для исполнения: значения секретов в открытом виде.

    env — пришедший из браузера или с диска список; пустые секреты берутся из stored (сохранённого проекта).
    """
    stored_map = {e.get("key"): e for e in (stored or []) if e.get("secret")}
    out = []
    for e in env or []:
        e = {k: v for k, v in e.items() if k not in ("has_value", "value_enc")} | (
            {"value_enc": e["value_enc"]} if e.get("value_enc") else {})
        if e.get("secret") and not e.get("value"):
            token = e.get("value_enc") or stored_map.get(e.get("key"), {}).get("value_enc")
            e["value"] = decrypt(token) if token else ""
        e.pop("value_enc", None)
        out.append(e)
    return out
