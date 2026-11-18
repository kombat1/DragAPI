"""Пользователи платформы, сессии и права на проекты.

data/users.json     пользователи (пароли — scrypt)
data/sessions.json  сессии: хранится только SHA-256 токена, сам токен — в HttpOnly-cookie
data/acl.json       права: {pid: {"owner": user_id, "members": {user_id: "editor" | "viewer"}}}

Роли в проекте: owner > editor > viewer. Администратор платформы имеет права владельца везде.
Выключить вход (одиночная локальная работа): `python -m app --no-auth`, DRAGAPI_AUTH=off или
auth = false в dragapi.toml — но только пока не заведено ни одного пользователя. Если пользователи есть,
сервер без входа не запустится, а вход остаётся обязательным при любых настройках.
"""

from __future__ import annotations

import base64
import hashlib
import hmac
import json
import os
import re
import secrets
import threading
import time
import uuid
from pathlib import Path
from .paths import DATA as _DATA

DATA = _DATA
USERNAME_RE = re.compile(r"^[A-Za-z0-9_.-]{3,32}$")
SESSION_TTL = 14 * 24 * 3600
COOKIE = "dragapi_session"
RANK = {"viewer": 1, "editor": 2, "owner": 3}
LOGIN_WINDOW, LOGIN_MAX_FAILS = 60, 5


class AuthError(Exception):
    def __init__(self, message: str, status: int = 400):
        super().__init__(message)
        self.status = status


def off_requested() -> tuple[bool, str]:
    """Просили ли выключить вход и откуда: флаг --no-auth / DRAGAPI_AUTH или auth = false в dragapi.toml."""
    env = os.environ.get("DRAGAPI_AUTH", "").strip().lower()
    if env:
        return env in ("off", "0", "false", "no"), "флаг --no-auth (переменную DRAGAPI_AUTH)"
    from . import config
    if config.load().get("auth") is False:
        return True, f"auth = false из {config.path()}"
    return False, ""


def enabled() -> bool:
    """Вход выключается только в платформе без пользователей; как только они есть — вход обязателен."""
    return not (off_requested()[0] and store.setup_required())


def check_startup():
    """Отказ запускаться без входа, если пользователи уже заведены."""
    off, source = off_requested()
    if off and not store.setup_required():
        raise AuthError(
            f"запуск без авторизации запрещён: в платформе уже есть пользователи ({len(store.users())}). "
            f"Уберите {source} и войдите под своей учётной записью.")


def hash_password(password: str) -> str:
    salt = os.urandom(16)
    digest = hashlib.scrypt(password.encode(), salt=salt, n=2**14, r=8, p=1, dklen=32)
    return "scrypt$16384$8$1$" + base64.b64encode(salt).decode() + "$" + base64.b64encode(digest).decode()


def verify_password(password: str, stored: str) -> bool:
    try:
        _, n, r, p, salt, digest = stored.split("$")
        got = hashlib.scrypt(password.encode(), salt=base64.b64decode(salt), n=int(n), r=int(r), p=int(p),
                             dklen=len(base64.b64decode(digest)))
        return hmac.compare_digest(got, base64.b64decode(digest))
    except (ValueError, TypeError):
        return False


def _token_hash(token: str) -> str:
    return hashlib.sha256(token.encode()).hexdigest()


class AuthStore:
    def __init__(self, root: Path | None = None):
        self.root = root or DATA
        self.lock = threading.RLock()
        self.fails: dict[str, list[float]] = {}

    # --- файлы ---

    def _read(self, name: str, default):
        try:
            return json.loads((self.root / name).read_text("utf-8"))
        except (OSError, json.JSONDecodeError):
            return default

    def _write(self, name: str, data):
        self.root.mkdir(parents=True, exist_ok=True)
        path = self.root / name
        tmp = path.with_suffix(".tmp")
        fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            json.dump(data, f, ensure_ascii=False, indent=1)
        tmp.replace(path)

    # --- пользователи ---

    def users(self) -> list[dict]:
        return self._read("users.json", [])

    def public(self, user: dict | None) -> dict | None:
        return None if user is None else {k: user[k] for k in ("id", "username", "role", "created")}

    def setup_required(self) -> bool:
        return not self.users()

    def create_user(self, username: str, password: str, role: str = "user") -> dict:
        username = (username or "").strip()
        if not USERNAME_RE.match(username):
            raise AuthError("имя пользователя: 3–32 символа, латиница, цифры, _ . -")
        if len(password or "") < 8:
            raise AuthError("пароль — не короче 8 символов")
        if role not in ("admin", "user"):
            raise AuthError("роль: admin или user")
        with self.lock:
            users = self.users()
            if any(u["username"].lower() == username.lower() for u in users):
                raise AuthError("такой пользователь уже есть", 409)
            user = {"id": uuid.uuid4().hex[:12], "username": username, "role": role,
                    "password": hash_password(password), "created": time.time()}
            users.append(user)
            self._write("users.json", users)
            return user

    def get_user(self, user_id: str) -> dict | None:
        return next((u for u in self.users() if u["id"] == user_id), None)

    def delete_user(self, user_id: str):
        with self.lock:
            users = [u for u in self.users() if u["id"] != user_id]
            if not any(u["role"] == "admin" for u in users):
                raise AuthError("нельзя удалить последнего администратора")
            self._write("users.json", users)
            sessions = {k: v for k, v in self._read("sessions.json", {}).items() if v["user_id"] != user_id}
            self._write("sessions.json", sessions)
            acl = self._read("acl.json", {})
            for entry in acl.values():
                entry.get("members", {}).pop(user_id, None)
            self._write("acl.json", acl)

    def set_password(self, user_id: str, password: str):
        if len(password or "") < 8:
            raise AuthError("пароль — не короче 8 символов")
        with self.lock:
            users = self.users()
            for u in users:
                if u["id"] == user_id:
                    u["password"] = hash_password(password)
            self._write("users.json", users)
            # все сессии пользователя завершаются
            sessions = {k: v for k, v in self._read("sessions.json", {}).items() if v["user_id"] != user_id}
            self._write("sessions.json", sessions)

    # --- сессии ---

    def login(self, username: str, password: str, ip: str) -> tuple[str, dict]:
        now = time.time()
        recent = [t for t in self.fails.get(ip, []) if now - t < LOGIN_WINDOW]
        if len(recent) >= LOGIN_MAX_FAILS:
            raise AuthError("слишком много попыток — подождите минуту", 429)
        user = next((u for u in self.users() if u["username"].lower() == (username or "").strip().lower()), None)
        if user is None or not verify_password(password or "", user["password"]):
            if user is None:
                verify_password(password or "", hash_password("timing"))  # одинаковое время ответа
            self.fails[ip] = recent + [now]
            raise AuthError("неверное имя пользователя или пароль", 401)
        self.fails.pop(ip, None)
        token = secrets.token_urlsafe(32)
        with self.lock:
            sessions = {k: v for k, v in self._read("sessions.json", {}).items() if v["expires"] > now}
            sessions[_token_hash(token)] = {"user_id": user["id"], "expires": now + SESSION_TTL}
            self._write("sessions.json", sessions)
        return token, user

    def user_by_token(self, token: str | None) -> dict | None:
        if not token:
            return None
        session = self._read("sessions.json", {}).get(_token_hash(token))
        if not session or session["expires"] < time.time():
            return None
        return self.get_user(session["user_id"])

    def logout(self, token: str | None):
        if token:
            with self.lock:
                sessions = self._read("sessions.json", {})
                sessions.pop(_token_hash(token), None)
                self._write("sessions.json", sessions)

    # --- права на проекты ---

    def acl(self) -> dict:
        return self._read("acl.json", {})

    def role_for(self, user: dict | None, pid: str) -> str | None:
        if not enabled():
            return "owner"
        if user is None:
            return None
        if user["role"] == "admin":
            return "owner"
        entry = self.acl().get(pid)
        if not entry:
            return None
        if entry.get("owner") == user["id"]:
            return "owner"
        return entry.get("members", {}).get(user["id"])

    def set_owner(self, pid: str, user_id: str):
        with self.lock:
            acl = self.acl()
            acl.setdefault(pid, {"members": {}})["owner"] = user_id
            self._write("acl.json", acl)

    def claim_orphans(self, pids: list[str], user_id: str):
        """Проекты без владельца (созданные до включения входа) достаются этому пользователю."""
        with self.lock:
            acl = self.acl()
            for pid in pids:
                if not acl.get(pid, {}).get("owner"):
                    acl.setdefault(pid, {"members": {}})["owner"] = user_id
            self._write("acl.json", acl)

    def members(self, pid: str) -> dict:
        entry = self.acl().get(pid, {})
        return {"owner": entry.get("owner"), "members": entry.get("members", {})}

    def set_members(self, pid: str, members: dict[str, str]):
        known = {u["id"] for u in self.users()}
        clean = {}
        for uid, role in members.items():
            if uid not in known:
                raise AuthError(f"нет пользователя {uid}")
            if role not in ("editor", "viewer"):
                raise AuthError("роль участника: editor или viewer")
            clean[uid] = role
        with self.lock:
            acl = self.acl()
            entry = acl.setdefault(pid, {"members": {}})
            clean.pop(entry.get("owner"), None)
            entry["members"] = clean
            self._write("acl.json", acl)

    def forget_project(self, pid: str):
        with self.lock:
            acl = self.acl()
            acl.pop(pid, None)
            self._write("acl.json", acl)


store = AuthStore()


def required_level(method: str, rest: str) -> str:
    """Какая роль нужна для запроса к /api/projects/<pid><rest>."""
    if method in ("GET", "HEAD"):
        return "viewer"
    if method == "POST" and rest in ("/libraries/status", "/schema/plan"):
        return "viewer"  # только чтение
    if (method == "DELETE" and rest in ("", "/")) or rest.startswith("/members"):
        return "owner"
    return "editor"
