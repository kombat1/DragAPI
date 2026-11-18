"""DragAPI — сервер платформы.

  /api/...           управление проектами, проверка графа, запуск, экспорт
  /run/<id>/...      «живое» API запущенного проекта (+ /run/<id>/docs — Swagger)
"""

from __future__ import annotations

import io
import re
import zipfile
from collections import deque
from contextlib import asynccontextmanager
from pathlib import Path

import time
import uuid

from fastapi import FastAPI, HTTPException, Request, WebSocket
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse, JSONResponse as FastJSON, Response
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel
from starlette.responses import JSONResponse
from starlette.routing import Mount, Router

import asyncio

from . import auth, automation, bots, codegen, sandbox, storage, testing, vault
from .agents import LOCAL_ID, agent_bundle, hub
from .config import ConfigError
from .deploy import deployments, docker_available
from .pyenv import envs, validate_packages
from .blocks import catalog
from .graph import CompiledProject, GraphError, compile_project
from .interpreter import build_app
from .firewall import FirewallSettings
from .runtime import Database, DatabaseError, IngressFirewall

@asynccontextmanager
async def lifespan(_app):
    try:
        auth.check_startup()
    except (auth.AuthError, ConfigError) as e:
        raise SystemExit(f"DragAPI: {e}") from None
    if not auth.enabled():
        print("DragAPI: вход выключен — платформа открыта всем, кто может достучаться до сервера")
    storage.seed_examples()
    migrated = storage.migrate_secrets()
    if migrated:
        print(f"DragAPI: зашифрованы секреты в {migrated} файл(ах) проектов и истории")
    loop = asyncio.get_running_loop()
    hub.loop = loop
    hub.on_network = lambda pid, events: [runner.fw_event(pid, e) for e in events]
    hub.load_enabled()
    await hub.start_local(loop)
    if hub.local_agent is not None:
        print(f"DragAPI: локальный агент автоматизации готов (аварийная остановка: "
              f"{hub.conns[LOCAL_ID].info.get('stop_hotkey')})")
    elif hub.local_error:
        print(f"DragAPI: локальный агент автоматизации недоступен — {hub.local_error}")
    hub.scheduler_task = asyncio.ensure_future(hub.scheduler())
    bots.manager.hub, bots.manager.loop = hub, loop
    await bots.manager.autostart()
    yield
    bots.manager.stop_all()
    hub.scheduler_task.cancel()
    hub.telegram.stop_all()
    hub.stop_local()


app = FastAPI(title="DragAPI", version="0.4.0", lifespan=lifespan)

# ---------- вход и права ----------

PUBLIC_API = {"/api/auth/me", "/api/auth/login", "/api/auth/setup", "/api/auth/logout"}
PROJECT_PATH = re.compile(r"^/api/projects/([^/]+)(/.*)?$")


@app.middleware("http")
async def access_control(request: Request, call_next):
    path = request.url.path
    request.state.user = None
    if not path.startswith("/api/"):
        return await call_next(request)  # фронтенд и превью-API /run/... — публичные
    if request.method not in ("GET", "HEAD", "OPTIONS") and request.headers.get("x-dragapi") != "1":
        # браузер не даст чужому сайту выставить этот заголовок — защита от CSRF
        return FastJSON({"detail": "нет заголовка X-DragAPI"}, status_code=403)
    if not auth.enabled():
        if path.startswith("/api/users") or path == "/api/auth/setup":
            # пользователь, заведённый при выключенном входе, сразу заблокировал бы текущий сеанс
            return FastJSON({"detail": "вход выключен: чтобы завести пользователей, запустите сервер со входом"},
                            status_code=409)
        return await call_next(request)
    user = auth.store.user_by_token(request.cookies.get(auth.COOKIE))
    request.state.user = user
    if path in PUBLIC_API:
        return await call_next(request)
    if user is None:
        return FastJSON({"detail": "нужно войти"}, status_code=401)
    if path.startswith("/api/users") and not path.startswith("/api/users/brief") and user["role"] != "admin":
        return FastJSON({"detail": "только для администратора"}, status_code=403)
    if path.startswith("/api/agents") and request.method not in ("GET", "HEAD") and user["role"] != "admin":
        return FastJSON({"detail": "агентами управляет администратор"}, status_code=403)
    m = PROJECT_PATH.match(path)
    if m:
        role = auth.store.role_for(user, m.group(1))
        if role is None:
            return FastJSON({"detail": "проект не найден"}, status_code=404)  # не раскрываем существование
        need = auth.required_level(request.method, m.group(2) or "")
        if auth.RANK[role] < auth.RANK[need]:
            return FastJSON({"detail": f"недостаточно прав: нужна роль {need}, у вас {role}"}, status_code=403)
        request.state.role = role
    return await call_next(request)


def _user(request: Request) -> dict | None:
    return getattr(request.state, "user", None)


class Credentials(BaseModel):
    username: str
    password: str


class NewUser(Credentials):
    role: str = "user"


class PasswordIn(BaseModel):
    password: str
    old_password: str | None = None


def _auth_error(e: auth.AuthError):
    raise HTTPException(e.status, str(e)) from None


def _set_session(response: Response, token: str):
    response.set_cookie(auth.COOKIE, token, max_age=auth.SESSION_TTL, httponly=True, samesite="lax",
                        secure=False, path="/")


@app.get("/api/auth/me")
def auth_me(request: Request):
    if not auth.enabled():
        return {"auth": False, "user": None, "setup_required": False}
    return {"auth": True, "user": auth.store.public(_user(request)), "setup_required": auth.store.setup_required()}


@app.post("/api/auth/setup")
def auth_setup(body: Credentials, response: Response, request: Request):
    """Первый запуск: создать администратора. Все существующие проекты переходят к нему."""
    if not auth.store.setup_required():
        raise HTTPException(409, "администратор уже создан")
    try:
        user = auth.store.create_user(body.username, body.password, "admin")
        token, _ = auth.store.login(body.username, body.password, request.client.host if request.client else "")
    except auth.AuthError as e:
        _auth_error(e)
    auth.store.claim_orphans([p["id"] for p in storage.list_projects()], user["id"])
    _set_session(response, token)
    return {"user": auth.store.public(user)}


@app.post("/api/auth/login")
def auth_login(body: Credentials, response: Response, request: Request):
    try:
        token, user = auth.store.login(body.username, body.password, request.client.host if request.client else "")
    except auth.AuthError as e:
        _auth_error(e)
    _set_session(response, token)
    return {"user": auth.store.public(user)}


@app.post("/api/auth/logout")
def auth_logout(response: Response, request: Request):
    auth.store.logout(request.cookies.get(auth.COOKIE))
    response.delete_cookie(auth.COOKIE, path="/")
    return {"ok": True}


@app.post("/api/auth/password")
def auth_password(body: PasswordIn, request: Request):
    user = _user(request)
    if user is None:
        raise HTTPException(400, "вход выключен")
    if not auth.verify_password(body.old_password or "", user["password"]):
        raise HTTPException(403, "текущий пароль неверен")
    try:
        auth.store.set_password(user["id"], body.password)
    except auth.AuthError as e:
        _auth_error(e)
    return {"ok": True}


@app.get("/api/users")
def users_list():
    return [auth.store.public(u) for u in auth.store.users()]


@app.get("/api/users/brief")
def users_brief():
    """Имена пользователей — чтобы выбрать, с кем поделиться проектом."""
    return [{"id": u["id"], "username": u["username"]} for u in auth.store.users()]


@app.post("/api/users")
def users_create(body: NewUser):
    try:
        return auth.store.public(auth.store.create_user(body.username, body.password, body.role))
    except auth.AuthError as e:
        _auth_error(e)


@app.delete("/api/users/{user_id}")
def users_delete(user_id: str, request: Request):
    if _user(request) and _user(request)["id"] == user_id:
        raise HTTPException(400, "нельзя удалить самого себя")
    try:
        auth.store.delete_user(user_id)
    except auth.AuthError as e:
        _auth_error(e)
    return {"ok": True}


@app.put("/api/users/{user_id}/password")
def users_reset_password(user_id: str, body: PasswordIn):
    try:
        auth.store.set_password(user_id, body.password)
    except auth.AuthError as e:
        _auth_error(e)
    return {"ok": True}


class MembersIn(BaseModel):
    members: dict[str, str]  # user_id -> editor | viewer


@app.get("/api/projects/{pid}/members")
def project_members(pid: str):
    _load(pid)
    return auth.store.members(pid)


@app.put("/api/projects/{pid}/members")
def project_set_members(pid: str, body: MembersIn):
    _load(pid)
    try:
        auth.store.set_members(pid, body.members)
    except auth.AuthError as e:
        _auth_error(e)
    return auth.store.members(pid)


# ---------- проекты ----------


class LiveRunner:
    """ASGI-приложение, в которое на лету монтируются превью проектов."""

    def __init__(self):
        self.apps: dict[str, object] = {}  # FastAPI или IngressFirewall(FastAPI)
        self.dbs: dict[str, Database] = {}
        self.logs: dict[str, deque] = {}
        self.fw_logs: dict[str, deque] = {}
        self.router = Router()

    def mount(self, pid: str, sub, db: Database | None):
        self._dispose_db(pid)
        self.apps[pid] = sub
        if db is not None:
            self.dbs[pid] = db
        self._rebuild()

    def unmount(self, pid: str):
        self.apps.pop(pid, None)
        self._dispose_db(pid)
        self._rebuild()

    def _dispose_db(self, pid: str):
        old = self.dbs.pop(pid, None)
        if old is not None:
            old.dispose()

    def log(self, pid: str) -> deque:
        return self.logs.setdefault(pid, deque(maxlen=50))

    def fw_log(self, pid: str) -> deque:
        """Журнал firewall: исходящие соединения кода и входящие запросы к API."""
        return self.fw_logs.setdefault(pid, deque(maxlen=300))

    def fw_event(self, pid: str, event: dict):
        self.fw_log(pid).appendleft({"time": time.time(), **event})

    def _rebuild(self):
        self.router = Router(routes=[Mount(f"/{pid}", app=a) for pid, a in self.apps.items()])

    async def __call__(self, scope, receive, send):
        if scope["type"] == "http" and not self._has_project(scope):
            await JSONResponse({"detail": "проект не запущен — нажмите «Запустить»"}, 404)(scope, receive, send)
            return
        await self.router(scope, receive, send)

    def _has_project(self, scope) -> bool:
        path = scope.get("path", "")
        root = scope.get("root_path", "")
        rest = path[len(root):] if root and path.startswith(root) else path
        return rest.strip("/").split("/", 1)[0] in self.apps


runner = LiveRunner()


class GraphIn(BaseModel):
    name: str
    kind: str = "api"
    automation: dict = {}
    telegram: dict = {}
    bot: dict = {}
    nodes: list[dict] = []
    edges: list[dict] = []
    models: list[dict] = []
    env: list[dict] = []
    sandbox: dict = {}
    libraries: dict = {}
    firewall: dict = {}
    tests: list[dict] = []
    viewport: dict | None = None


class TestRequestIn(BaseModel):
    graph: GraphIn
    endpoint: str          # id блока эндпоинта
    request: dict = {}     # {path, query, headers, body_type, body, form}


class TestsRunIn(BaseModel):
    graph: GraphIn
    tests: list[dict] | None = None   # по умолчанию — все тесты из графа
    reset_data: bool = False          # очистить превью-БД перед прогоном


class CreateIn(BaseModel):
    name: str = "Новый проект"
    kind: str = "api"


def _load(pid: str) -> dict:
    try:
        return storage.load(pid)
    except storage.NotFound:
        raise HTTPException(404, "проект не найден") from None


def _compile(project: dict) -> CompiledProject:
    if project.get("kind") in ("automation", "bot"):
        raise HTTPException(400, {"message": "это не API-проект: у него нет HTTP-API для запуска, "
                                             "экспорта и деплоя", "issues": []})
    try:
        return compile_project(project)
    except GraphError as e:
        raise HTTPException(400, {"message": "в графе есть ошибки", "issues": [i.as_dict() for i in e.issues]})


def _open_db(pid: str, compiled: CompiledProject) -> Database | None:
    if not compiled.uses_db:
        return None
    try:
        return Database(storage.db_url(pid), [m.as_dict() for m in compiled.models])
    except Exception as e:  # noqa: BLE001 — миграция могла не пройти: показываем причину
        raise HTTPException(400, {"message": f"не удалось подготовить базу данных: {e}", "issues": []})


@app.get("/api/blocks")
def blocks():
    return catalog()


class SandboxRunIn(BaseModel):
    code: str
    data: dict = {}
    env: dict[str, str] = {}
    timeout: float = 2
    memory_mb: int = 256
    sandbox: dict = {}
    firewall: dict = {}


class DescribeIn(BaseModel):
    sandbox: dict = {}
    firewall: dict = {}


class FirewallCheckIn(BaseModel):
    firewall: dict = {}
    direction: str = "out"   # out — код блоков → сеть; in — клиент → API
    target: str              # out: host:port или URL; in: IP-адрес


class LibrariesIn(BaseModel):
    packages: list[str] = []
    only_binary: bool = True
    sandbox: dict = {}


def _settings(raw: dict) -> sandbox.SandboxSettings:
    settings, errors = sandbox.SandboxSettings.parse(raw)
    if errors:
        raise HTTPException(400, {"message": "; ".join(errors), "issues": []})
    return settings


def _firewall(raw: dict, settings: sandbox.SandboxSettings) -> FirewallSettings:
    fw, errors = FirewallSettings.parse(raw, legacy_network=settings.network)
    errors = [e for e in errors if "API будет недоступно" not in e]  # это предупреждение, а не ошибка ввода
    if errors:
        raise HTTPException(400, {"message": "; ".join(errors), "issues": []})
    return fw


@app.get("/api/sandbox")
def sandbox_info():
    return sandbox.describe()


@app.post("/api/sandbox/describe")
def sandbox_describe(body: DescribeIn):
    """Что получится из настроек песочницы и firewall проекта (ещё не сохранённых)."""
    settings = _settings(body.sandbox)
    return sandbox.describe(settings, _firewall(body.firewall, settings))


@app.get("/api/whoami")
def whoami(request: Request):
    """IP, с которого браузер обращается к платформе, — чтобы добавить себя во входящие правила."""
    return {"ip": request.client.host if request.client else None}


@app.post("/api/firewall/check")
async def firewall_check(body: FirewallCheckIn):
    """«Проверить адрес» во вкладке Firewall: что решат правила для этого адреса."""
    settings = sandbox.SandboxSettings()
    fw = _firewall(body.firewall, settings)
    target = body.target.strip()
    if body.direction == "in":
        rules = fw.ingress.runtime_rules()
        allowed, reason = IngressFirewall(None, rules, fw.ingress.default).decide(target)
        if not fw.ingress.enabled:
            reason += " (входящий firewall выключен — сейчас разрешено всё)"
            allowed = True
        return {"allowed": allowed, "reason": reason}
    if "://" in target:
        from urllib.parse import urlsplit
        url = urlsplit(target)
        host, port = url.hostname or "", url.port or (443 if url.scheme == "https" else 80)
    else:
        host, _, port_s = target.rpartition(":") if ":" in target.strip("[]").replace("::", "") or target.count(":") == 1 else (target, "", "")
        if not host:
            host, port_s = target, ""
        port = int(port_s) if port_s.isdigit() else 443
    mode = fw.egress.mode
    if mode != "filtered":
        return {"allowed": mode == "open", "reason": {"off": "сеть для кода отключена",
                                                      "open": "сеть без ограничений — правила не применяются"}[mode],
                "host": host, "port": port}
    verdict = await fw.egress.decide(host, port)
    return {"allowed": verdict.allowed, "reason": verdict.reason, "ip": verdict.ip, "host": host, "port": port}


@app.get("/api/projects/{pid}/firewall/log")
def firewall_log(pid: str):
    return list(runner.fw_log(pid))


@app.delete("/api/projects/{pid}/firewall/log")
def firewall_log_clear(pid: str):
    runner.fw_log(pid).clear()
    return {"ok": True}


@app.post("/api/projects/{pid}/sandbox/run")
async def sandbox_run(pid: str, body: SandboxRunIn):
    """«Проверить» в редакторе блока кода: run(data, env) в песочнице и окружении проекта."""
    _load(pid)
    if len(body.code) > 20_000:
        raise HTTPException(400, "код слишком длинный")
    limits = sandbox.Limits(min(max(body.timeout, 0.1), 10), min(max(body.memory_mb, 32), 1024))
    settings = _settings(body.sandbox)
    env = dict(body.env)
    stored = _with_secrets(pid, {"env": _load(pid).get("env") or []})["env"]
    for e in stored:  # секреты браузер не знает — подставляем сохранённые значения
        if e.get("secret") and not env.get(e["key"]):
            env[e["key"]] = e["value"]
    result = await sandbox.run_code(body.code, body.data, env, limits, settings, envs.venv(pid),
                                    _firewall(body.firewall, settings))
    for event in result.network or []:
        runner.fw_event(pid, {**event, "source": "проверка кода"})
    return result.as_dict()


@app.post("/api/projects/{pid}/libraries/status")
def libraries_status(pid: str, body: LibrariesIn):
    _load(pid)
    packages, errors = validate_packages(body.packages)
    return {**envs.status(pid, packages, body.only_binary), "errors": errors}


@app.post("/api/projects/{pid}/libraries/install")
async def libraries_install(pid: str, body: LibrariesIn):
    _load(pid)
    packages, errors = validate_packages(body.packages)
    if errors:
        raise HTTPException(400, {"message": "; ".join(errors), "issues": []})
    use_firejail = sandbox.resolve_backend(_settings(body.sandbox)) == "firejail"
    try:
        envs.start_install(pid, packages, body.only_binary, use_firejail)
    except RuntimeError as e:
        raise HTTPException(409, str(e)) from None
    return envs.status(pid, packages, body.only_binary)


@app.delete("/api/projects/{pid}/libraries")
def libraries_remove(pid: str):
    _load(pid)
    envs.remove(pid)
    return {"ok": True}


@app.get("/api/projects")
def projects(request: Request):
    user = _user(request)
    out = []
    for p in storage.list_projects():
        role = auth.store.role_for(user, p["id"])
        if role:
            out.append({**p, "role": role})
    return out


@app.post("/api/projects")
def create_project(body: CreateIn, request: Request):
    if body.kind not in ("api", "automation", "bot"):
        raise HTTPException(400, "тип проекта: api, automation или bot")
    default = {"automation": "Новый сценарий", "bot": "Новый бот"}.get(body.kind, "Новый проект")
    project = storage.create(body.name.strip() or default, body.kind)
    if _user(request):
        auth.store.set_owner(project["id"], _user(request)["id"])
    return project


def _with_secrets(pid: str, graph: dict) -> dict:
    """Граф с расшифрованными значениями секретов: пустые берутся из сохранённого проекта."""
    stored = storage.load(pid).get("env") if storage.ID_RE.match(pid) else []
    try:
        return {**graph, "env": vault.unseal_env(graph.get("env"), stored)}
    except vault.VaultError as e:
        raise HTTPException(400, {"message": str(e), "issues": []}) from None


@app.get("/api/projects/{pid}")
def get_project(pid: str, request: Request):
    project = _load(pid)
    role = getattr(request.state, "role", None) or auth.store.role_for(_user(request), pid)
    return _public_project({**project, "running": pid in runner.apps, "my_role": role})


def _public_project(project: dict) -> dict:
    out = {**project, "env": vault.mask_env(project.get("env"))}
    if isinstance(out.get("automation"), dict):
        auto = dict(out["automation"])
        auto["has_hook_token"] = bool(auto.pop("hook_token_hash", None))
        auto.pop("enabled_by", None)
        out["automation"] = auto
    return out


def _prepare_automation(pid: str, data: dict, stored: dict, user: dict | None) -> dict:
    """Сохранение сценария: служебные поля — с сервера; включить его может только тот, кому доступен агент."""
    if (stored.get("kind") or "api") != "automation":
        return {**data, "kind": stored.get("kind") or "api"}
    old = stored.get("automation") or {}
    auto = {k: v for k, v in (data.get("automation") or {}).items() if k not in ("has_hook_token", "enabled_by",
                                                                             "hook_token_hash")}
    if old.get("hook_token_hash"):
        auto["hook_token_hash"] = old["hook_token_hash"]
    agent_id = auto.get("agent") or LOCAL_ID
    if auto.get("enabled"):
        if not hub.can_use(user, agent_id):
            raise HTTPException(403, "включить сценарий может только тот, кому доступен выбранный агент")
        auto["enabled_by"] = user["id"] if user else None
    return {**data, "kind": "automation", "automation": auto}


@app.put("/api/projects/{pid}")
def save_project(pid: str, body: GraphIn, request: Request):
    stored = _load(pid)
    data = _prepare_automation(pid, body.model_dump(), stored, _user(request))
    saved = storage.save(pid, data)
    if saved.get("kind") == "automation" or (stored.get("automation") or {}).get("enabled"):
        hub.refresh_project(pid, saved)
    out = _public_project(saved)
    if saved.get("kind") == "bot":
        out["bot_reload_error"] = bots.manager.reload(pid, saved)
    return out


@app.delete("/api/projects/{pid}")
async def delete_project(pid: str):
    if docker_available():
        await deployments.stop(pid, remove_data=True)
    runner.unmount(pid)
    envs.remove(pid)
    hub.refresh_project(pid, None)
    await bots.manager.stop(pid)
    bots.manager.emulators.pop(pid, None)
    (bots.BOTS_DIR / f"{pid}.json").unlink(missing_ok=True)
    try:
        storage.delete(pid)
    except storage.NotFound:
        raise HTTPException(404, "проект не найден") from None
    auth.store.forget_project(pid)
    return {"ok": True}


@app.get("/api/projects/{pid}/versions")
def project_versions(pid: str):
    _load(pid)
    return storage.versions(pid)


@app.post("/api/projects/{pid}/versions/{version}/restore")
def restore_version(pid: str, version: str):
    _load(pid)
    try:
        old = storage.load_version(pid, version)
    except storage.NotFound:
        raise HTTPException(404, "версия не найдена") from None
    current = _load(pid)
    old = {**old, "kind": current.get("kind") or "api"}
    if old["kind"] == "automation":  # откат не включает сценарий заново от чужого имени
        old["automation"] = {**(old.get("automation") or {}), "enabled": False,
                             "hook_token_hash": (current.get("automation") or {}).get("hook_token_hash")}
    saved = storage.save(pid, old)
    hub.refresh_project(pid, saved)
    return _public_project(saved)


@app.post("/api/validate")
def validate(body: GraphIn, project_id: str | None = None):
    """Проверка графа без сохранения — фронтенд вызывает её при каждом изменении."""
    if body.kind == "bot":
        try:
            compiled_bot = bots.compile_bot(body.model_dump())
        except GraphError as e:
            return {"ok": False, "issues": [i.as_dict() for i in e.issues]}
        return {"ok": True, "issues": [w.as_dict() for w in compiled_bot.warnings], "handlers": compiled_bot.summary()}
    if body.kind == "automation":
        try:
            compiled = automation.compile_automation(body.model_dump())
        except GraphError as e:
            return {"ok": False, "issues": [i.as_dict() for i in e.issues]}
        return {"ok": True, "issues": [w.as_dict() for w in compiled.warnings],
                "scenarios": [s.trigger.as_dict() for s in compiled.scenarios]}
    try:
        compiled = compile_project(body.model_dump())
    except GraphError as e:
        return {"ok": False, "issues": [i.as_dict() for i in e.issues]}
    warnings = [w.as_dict() for w in compiled.warnings]
    if project_id and storage.ID_RE.match(project_id) and \
            not envs.is_current(project_id, compiled.packages, compiled.only_binary):
        warnings.append({"node_id": None, "level": "warning",
                         "message": "библиотеки проекта не установлены или изменены — "
                                    "установите их в «Настройки проекта → Библиотеки»"})
    return {
        "ok": True,
        "issues": warnings,
        "routes": [{"method": p.method, "path": p.path, "node_id": p.node_id} for p in compiled.plans],
    }


@app.post("/api/projects/{pid}/run")
def run_project(pid: str):
    project = _with_secrets(pid, _load(pid))
    compiled = _compile(project)
    db = _open_db(pid, compiled)
    sub = build_app(compiled, db, runner.log(pid), lambda: envs.venv(pid),
                    on_network=lambda events: [runner.fw_event(pid, {**e, "source": "запрос"}) for e in events])
    ingress = compiled.firewall.ingress
    if ingress.enabled:
        sub = IngressFirewall(sub, ingress.runtime_rules(), ingress.default, ingress.trust_proxy,
                              on_event=lambda e: runner.fw_event(pid, e))
    runner.mount(pid, sub, db)
    return {
        "ok": True,
        "base_url": f"/run/{pid}",
        "docs_url": f"/run/{pid}/docs",
        "routes": [{"method": p.method, "path": p.path} for p in compiled.plans],
        "issues": [w.as_dict() for w in compiled.warnings],
        "migrations": db.applied if db else [],
    }


class DeployIn(BaseModel):
    port: int | None = None
    public: bool = False


@app.get("/api/projects/{pid}/deploy")
async def deploy_status(pid: str):
    _load(pid)
    return await deployments.status(pid)


@app.post("/api/projects/{pid}/deploy")
async def deploy_start(pid: str, body: DeployIn):
    """Собрать образ из экспорта и запустить контейнер (в фоне, статус — GET)."""
    if not docker_available():
        raise HTTPException(400, {"message": "Docker недоступен на этой машине", "issues": []})
    if body.port is not None and not 1024 <= body.port <= 65535:
        raise HTTPException(400, {"message": "порт: 1024–65535", "issues": []})
    project = _with_secrets(pid, _load(pid))
    compiled = _compile(project)
    env = {e.key: e.value for e in compiled.env}
    try:
        deployments.start(pid, codegen.generate_files(compiled), env, body.port, body.public)
    except RuntimeError as e:
        raise HTTPException(409, str(e)) from None
    return await deployments.status(pid)


@app.delete("/api/projects/{pid}/deploy")
async def deploy_stop(pid: str, remove_data: bool = False):
    _load(pid)
    await deployments.stop(pid, remove_data)
    return await deployments.status(pid)


@app.get("/api/projects/{pid}/deploy/logs")
async def deploy_logs(pid: str, tail: int = 200):
    _load(pid)
    return {"logs": await deployments.logs(pid, tail)}


class SchemaIn(BaseModel):
    models: list[dict] = []


def _schema_db(pid: str, models: list[dict]) -> Database:
    from .graph import _compile_models
    errors = []
    specs = _compile_models(models, lambda _n, m: errors.append(m))
    if errors:
        raise HTTPException(400, {"message": "; ".join(errors), "issues": []})
    return Database(storage.db_url(pid), [m.as_dict() for m in specs], apply_migrations=False)


@app.post("/api/projects/{pid}/schema/plan")
def schema_plan(pid: str, body: SchemaIn):
    """Что изменится в превью-БД при следующем запуске (ничего не меняет)."""
    _load(pid)
    db = _schema_db(pid, body.models)
    try:
        return {"ops": db.plan()}
    finally:
        db.dispose()


@app.post("/api/projects/{pid}/schema/drop-extra")
def schema_drop_extra(pid: str, body: SchemaIn):
    """Удалить колонки, которых больше нет в моделях (данные в них теряются)."""
    _load(pid)
    db = _schema_db(pid, body.models)
    try:
        return {"applied": db.migrate(drop_extra=True)}
    finally:
        db.dispose()


class _Preview:
    """Приложение из текущего (возможно, несохранённого) графа — для тестовых запросов из редактора."""

    def __init__(self, pid: str, graph: GraphIn):
        self.pid = pid
        self.compiled = _compile(_with_secrets(pid, graph.model_dump()))
        self.db = _open_db(pid, self.compiled)
        self.log: deque = deque(maxlen=1)
        self.app = build_app(self.compiled, self.db, self.log, lambda: envs.venv(pid),
                             on_network=lambda events: [runner.fw_event(pid, {**e, "source": "тестовый запрос"})
                                                        for e in events])
        self.plans = {p.node_id: p for p in self.compiled.plans}

    def close(self):
        if self.db is not None:
            self.db.dispose()

    async def send(self, endpoint: str, request: dict) -> dict:
        plan = self.plans.get(endpoint)
        if plan is None:
            raise HTTPException(400, {"message": "эндпоинт не найден или его цепочка содержит ошибки", "issues": []})
        try:
            return await testing.send(self.app, plan, request, self.compiled.env_values, self.log)
        except testing.TemplateError as e:
            raise HTTPException(400, {"message": str(e), "issues": []}) from None


@app.post("/api/projects/{pid}/test-request")
async def test_request(pid: str, body: TestRequestIn):
    """«Отправить» в панели «Тесты»: запрос к текущему графу без сохранения и запуска."""
    _load(pid)
    preview = _Preview(pid, body.graph)
    try:
        return await preview.send(body.endpoint, body.request)
    finally:
        preview.close()


@app.post("/api/projects/{pid}/tests/run")
async def tests_run(pid: str, body: TestsRunIn):
    """«Прогнать все»: тест-кейсы по порядку (они могут зависеть друг от друга через данные)."""
    _load(pid)
    preview = _Preview(pid, body.graph)
    tests = body.tests if body.tests is not None else body.graph.tests
    results = []
    try:
        if body.reset_data and preview.db is not None:
            preview.db.clear_all()
        for t in tests:
            started = time.perf_counter()
            try:
                result = await preview.send(t.get("endpoint", ""), t.get("request") or {})
                problems = testing.check_expectation(result, t.get("expect") or {})
            except HTTPException as e:
                result, problems = None, [e.detail["message"] if isinstance(e.detail, dict) else str(e.detail)]
            results.append({"id": t.get("id"), "name": t.get("name"), "passed": not problems,
                            "problems": problems, "result": result,
                            "ms": round((time.perf_counter() - started) * 1000, 1)})
    finally:
        preview.close()
    return {"passed": sum(r["passed"] for r in results), "total": len(results), "results": results}


@app.post("/api/projects/{pid}/stop")
def stop_project(pid: str):
    runner.unmount(pid)
    return {"ok": True}


@app.get("/api/projects/{pid}/logs")
def project_logs(pid: str):
    return list(runner.log(pid))


@app.delete("/api/projects/{pid}/logs")
def clear_logs(pid: str):
    runner.log(pid).clear()
    return {"ok": True}


# --- данные превью-БД ---

def _data_db(pid: str) -> Database:
    db = runner.dbs.get(pid)
    if db is None:
        raise HTTPException(409, "запустите проект, чтобы увидеть данные")
    return db


@app.get("/api/projects/{pid}/data")
def data_overview(pid: str):
    db = _data_db(pid)
    return [{"model": name, "count": db.count(name)} for name in db.tables]


@app.get("/api/projects/{pid}/data/{model}")
def data_rows(pid: str, model: str, limit: int = 100):
    db = _data_db(pid)
    if model not in db.tables:
        raise HTTPException(404, "модель не найдена")
    return {
        "columns": [c.name for c in db.tables[model].columns],
        "rows": db.list(model, limit=limit, order_by="id", descending=True),
        "count": db.count(model),
    }


@app.delete("/api/projects/{pid}/data/{model}")
def data_clear(pid: str, model: str):
    db = _data_db(pid)
    if model not in db.tables:
        raise HTTPException(404, "модель не найдена")
    try:
        db.clear(model)
    except DatabaseError as e:
        raise HTTPException(409, str(e)) from None
    return {"ok": True}


@app.get("/api/projects/{pid}/export")
def export_project(pid: str, format: str = "zip"):
    project = _load(pid)
    compiled = _compile(project)
    files = codegen.generate_files(compiled)
    if format == "py":
        return Response(files["main.py"], media_type="text/x-python; charset=utf-8")
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as z:
        for path, content in files.items():  # main.py, blocks/, tests/, …
            z.writestr(f"{pid}/{path}", content)
        z.writestr(f"{pid}/README.md", f"# {project.get('name', pid)}\n\nСгенерировано DragAPI.\n\n"
                                       "```bash\npip install -r requirements.txt\n"
                                       "cp .env.example .env   # заполните секреты\n"
                                       "set -a; . ./.env; set +a\n"
                                       "uvicorn main:app --reload\n```\n")
    return Response(buf.getvalue(), media_type="application/zip",
                    headers={"Content-Disposition": f'attachment; filename="{pid}.zip"'})


# ---------- автоматизация ----------


def _automation_project(pid: str) -> dict:
    project = _load(pid)
    if project.get("kind") != "automation":
        raise HTTPException(400, "это не проект-автоматизация")
    return project


def _compile_automation(pid: str, graph: dict) -> automation.CompiledAutomation:
    try:
        return automation.compile_automation(_with_secrets(pid, graph))
    except GraphError as e:
        raise HTTPException(400, {"message": "в сценарии есть ошибки", "issues": [i.as_dict() for i in e.issues]})


def _agent_for(request: Request, compiled: automation.CompiledAutomation, agent: str | None) -> str:
    agent_id = agent or compiled.settings.agent or LOCAL_ID
    if not hub.can_use(_user(request), agent_id):
        raise HTTPException(403, "нет прав на этого агента")
    return agent_id


class AutoRunIn(BaseModel):
    scenario: str = "main"
    inputs: dict = {}
    agent: str | None = None
    graph: GraphIn | None = None     # текущий (несохранённый) граф из редактора


class RecordIn(BaseModel):
    agent: str | None = None
    countdown: int = 3


class AgentIn(BaseModel):
    name: str = ""
    users: list[str] | None = None


@app.get("/api/automation/catalog")
def automation_catalog():
    return automation.catalog()


@app.post("/api/projects/{pid}/automation/run")
async def automation_run(pid: str, body: AutoRunIn, request: Request):
    stored = _automation_project(pid)
    graph = {**body.graph.model_dump(), "kind": "automation"} if body.graph else stored
    compiled = _compile_automation(pid, graph)
    agent_id = _agent_for(request, compiled, body.agent)
    user = _user(request)
    try:
        run = await hub.start_run(pid, compiled, body.scenario, body.inputs, "manual",
                                  user["id"] if user else None, agent_id)
    except LookupError as e:
        raise HTTPException(404, str(e)) from None
    except ValueError as e:
        raise HTTPException(409, str(e)) from None
    return run.as_dict(after=0)


@app.get("/api/projects/{pid}/automation/runs")
def automation_runs(pid: str):
    _automation_project(pid)
    return [r.as_dict() for r in hub.project_history(pid)]


@app.get("/api/projects/{pid}/automation/runs/{run_id}")
def automation_run_status(pid: str, run_id: str, after: int = 0):
    run = hub.get_run(pid, run_id)
    if run is None:
        raise HTTPException(404, "запуск не найден")
    return run.as_dict(after=max(after, 0))


@app.post("/api/projects/{pid}/automation/runs/{run_id}/stop")
async def automation_run_stop(pid: str, run_id: str):
    run = hub.get_run(pid, run_id)
    if run is None:
        raise HTTPException(404, "запуск не найден")
    await hub.stop_run(run_id)
    return {"ok": True}


def _agent_from_project(request: Request, pid: str, agent: str | None) -> str:
    project = _automation_project(pid)
    agent_id = agent or (project.get("automation") or {}).get("agent") or LOCAL_ID
    if not hub.can_use(_user(request), agent_id):
        raise HTTPException(403, "нет прав на этого агента")
    return agent_id


@app.post("/api/projects/{pid}/automation/record")
async def automation_record_start(pid: str, body: RecordIn, request: Request):
    agent_id = _agent_from_project(request, pid, body.agent)
    try:
        rec = await hub.record_start(agent_id, min(max(body.countdown, 0), 10))
    except ValueError as e:
        raise HTTPException(409, str(e)) from None
    rec["pid"] = pid
    return rec


def _recording(pid: str, rec_id: str) -> dict:
    rec = hub.recordings.get(rec_id)
    if rec is None or rec.get("pid") != pid:
        raise HTTPException(404, "запись не найдена")
    return rec


@app.get("/api/projects/{pid}/automation/record/{rec_id}")
def automation_record_status(pid: str, rec_id: str):
    return _recording(pid, rec_id)


@app.post("/api/projects/{pid}/automation/record/{rec_id}/stop")
async def automation_record_stop(pid: str, rec_id: str):
    _recording(pid, rec_id)
    await hub.record_stop(rec_id)
    return {"ok": True}


@app.post("/api/projects/{pid}/automation/pick-point")
async def automation_pick_point(pid: str, body: RecordIn, request: Request):
    agent_id = _agent_from_project(request, pid, body.agent)
    try:
        point = await hub.pick_point(agent_id, min(max(body.countdown, 0), 10))
    except ValueError as e:
        raise HTTPException(409, str(e)) from None
    point["pid"] = pid
    return point


@app.get("/api/projects/{pid}/automation/pick-point/{req}")
def automation_pick_point_status(pid: str, req: str):
    point = hub.points.get(req)
    if point is None or point.get("pid") != pid:
        raise HTTPException(404, "запрос не найден")
    return point


@app.post("/api/projects/{pid}/automation/hook-token")
def automation_hook_token(pid: str):
    """Новый токен вебхуков сценариев (старый перестаёт работать). Показывается один раз."""
    import hashlib
    import secrets as _secrets
    project = _automation_project(pid)
    token = "dgh_" + _secrets.token_urlsafe(32)
    project["automation"] = {**(project.get("automation") or {}),
                             "hook_token_hash": hashlib.sha256(token.encode()).hexdigest()}
    storage.save(pid, project)
    return {"token": token}


class TelegramTestIn(BaseModel):
    telegram: dict = {}
    env: list[dict] = []
    send: bool = True


@app.post("/api/projects/{pid}/telegram/check")
async def telegram_check(pid: str, body: TelegramTestIn):
    """«Проверить бота»: имя бота, недавние чаты (чтобы добавить их в разрешённые) и пробное сообщение."""
    from . import telegram as tg
    _automation_project(pid)
    env = _with_secrets(pid, {"env": body.env})["env"]
    token_env = str(body.telegram.get("token_env") or "")
    token = next((e.get("value") for e in env if e.get("key") == token_env), "")
    if not token:
        raise HTTPException(400, {"message": f"переменная {token_env or '(не выбрана)'} пуста — задайте токен бота", "issues": []})
    out = {"username": None, "chats": [], "sent": [], "errors": [], "polling": hub.telegram.status(token)}
    try:
        out["username"] = (await tg.call(token, "getMe")).get("username")
    except tg.TelegramError as e:
        raise HTTPException(400, {"message": f"Telegram: {e}", "issues": []}) from None
    if out["polling"]:
        out["chats"] = out["polling"]["seen_chats"]
    else:  # бота сейчас никто не опрашивает — смотрим недавние сообщения, не подтверждая их
        try:
            seen = {}
            for upd in await tg.call(token, "getUpdates", timeout=0) or []:  # timeout=0: не ждать
                chat = ((upd.get("message") or {}).get("chat")) or {}
                if chat.get("id") is not None:
                    seen[str(chat["id"])] = chat.get("title") or chat.get("first_name") or chat.get("username") or ""
            out["chats"] = [{"id": k, "title": v} for k, v in seen.items()]
        except tg.TelegramError as e:
            out["errors"].append(str(e))
    if body.send:
        for chat in body.telegram.get("chats") or []:
            try:
                await hub.telegram.send(token, str(chat), "✅ DragAPI: бот подключён к проекту. Команды — /list")
                out["sent"].append(str(chat))
            except tg.TelegramError as e:
                out["errors"].append(f"{chat}: {e}")
    return out


# --- конструктор ботов ---

def _bot_project(pid: str) -> dict:
    project = _load(pid)
    if project.get("kind") != "bot":
        raise HTTPException(400, "это не проект-бот")
    return project


class EmulatorIn(BaseModel):
    graph: GraphIn | None = None
    text: str | None = None
    button: dict | None = None     # {message_id, data, text}
    pay: dict | None = None        # {message_id} — «Оплатить (тест)»
    as_role: str | None = None     # тестировать как admin / moderator / user
    group: bool = False            # писать в тестовую группу
    as_other: bool = False         # от имени другого участника (для проверки модерации)
    reply_to: int | None = None    # ответом на сообщение тест-чата
    file: dict | None = None       # {name, mime, size, preview} — прикреплённый файл
    contact: bool = False          # «поделиться номером»
    member: str | None = None      # join / leave — участник вошёл или вышел
    chat_admin: bool | None = None # «вы» — администратор тестовой группы
    reset: bool = False


@app.get("/api/bots/catalog")
def bots_catalog():
    return bots.catalog()


@app.get("/api/projects/{pid}/bot/status")
def bot_status(pid: str):
    _bot_project(pid)
    live = bots.manager.live.get(pid)
    return {**bots.manager.status(pid), "log": list(live.engine.log)[:150] if live else []}


@app.post("/api/projects/{pid}/bot/start")
async def bot_start(pid: str):
    _bot_project(pid)
    try:
        await bots.manager.start(pid)
    except GraphError as e:
        raise HTTPException(400, {"message": "в графе бота есть ошибки", "issues": [i.as_dict() for i in e.issues]})
    except (ValueError, vault.VaultError) as e:
        raise HTTPException(409, {"message": str(e), "issues": []}) from None
    return bot_status(pid)


@app.post("/api/projects/{pid}/bot/stop")
async def bot_stop(pid: str):
    _bot_project(pid)
    await bots.manager.stop(pid)
    return bot_status(pid)


@app.post("/api/projects/{pid}/bot/emulator")
async def bot_emulator(pid: str, body: EmulatorIn):
    """Тест-чат: сообщение или нажатие кнопки — бот по текущему (несохранённому) графу, без Telegram."""
    project = _bot_project(pid)
    graph = {**body.graph.model_dump(), "kind": "bot"} if body.graph else project
    try:
        compiled = bots.compile_bot(_with_secrets(pid, graph))
    except GraphError as e:
        raise HTTPException(400, {"message": "в графе бота есть ошибки", "issues": [i.as_dict() for i in e.issues]})
    engine = bots.manager.emulator(pid, compiled, reset=body.reset)
    if body.as_role:
        if body.as_role not in compiled.all_roles():
            raise HTTPException(400, f"неизвестная роль «{body.as_role}»")
        engine.store.set_role(bots.EMU_USER["id"], body.as_role)
    if body.chat_admin is not None:
        engine.transport.chat_admins = {"1": "administrator"} if body.chat_admin else {}
        engine.admin_cache.clear()
    media = None
    if body.file is not None:
        mime = str(body.file.get("mime") or "")
        kind = "photo" if mime.startswith("image/") else "video" if mime.startswith("video/") else \
            "audio" if mime.startswith("audio/") else "document"
        preview = body.file.get("preview")
        media = {"kind": kind, "file_id": "emu-" + uuid.uuid4().hex[:10], "name": str(body.file.get("name") or "file")[:200],
                 "size": body.file.get("size"), "mime": mime,
                 "preview": preview if isinstance(preview, str) and len(preview) < 2_000_000 else None}
    if any(x is not None for x in (body.text, body.button, body.pay, media, body.member)) or body.contact:
        await bots.emulate(engine, body.text, body.button, pay=body.pay, group=body.group, as_other=body.as_other,
                           reply_to=body.reply_to, media=media, contact=body.contact, member=body.member)
    return _emulator_state(engine)


def _emulator_state(engine) -> dict:
    return {"messages": engine.transport.messages, "log": list(engine.log)[:100],
            "keyboard": engine.transport.keyboard, "chat_admin": bool(engine.transport.chat_admins),
            "role": engine.store.role(bots.EMU_USER["id"], engine.compiled.admins),
            "roles": engine.compiled.all_roles(),
            "waiting": engine.waiters.get(f"{bots.EMU_CHAT}:{bots.EMU_USER['id']}") is not None,
            "saved": (engine.store.data["users"].get(str(bots.EMU_USER["id"])) or {}).get("saved", {})}


@app.get("/api/projects/{pid}/bot/emulator")
def bot_emulator_state(pid: str):
    _bot_project(pid)
    engine = bots.manager.emulators.get(pid)
    return _emulator_state(engine) if engine else {"messages": [], "log": [], "waiting": False, "saved": {}}


@app.get("/api/projects/{pid}/bot/users")
def bot_users(pid: str):
    project = _bot_project(pid)
    try:
        admins = bots.manager.compiled_for(pid, project).admins
    except (GraphError, vault.VaultError):
        admins = [str(a) for a in (project.get("bot") or {}).get("admins") or []]
    # итоговая роль: администраторы из настроек — admin независимо от сохранённой
    return [{**u, "role": "admin" if u["id"] in admins else u.get("role") or "user", "settings_admin": u["id"] in admins}
            for u in bots.manager.store(pid).users()]


class BotUserIn(BaseModel):
    role: str


@app.put("/api/projects/{pid}/bot/users/{uid}")
def bot_user_role(pid: str, uid: str, body: BotUserIn):
    """Сменить роль пользователя бота (в т.ч. admin и banned) из платформы."""
    project = _bot_project(pid)
    try:
        compiled = bots.manager.compiled_for(pid, project)
    except (GraphError, vault.VaultError) as e:
        raise HTTPException(400, f"исправьте ошибки в графе: {e}") from None
    if body.role not in compiled.all_roles():
        raise HTTPException(400, f"неизвестная роль «{body.role}»")
    if uid in compiled.admins and body.role != "admin":
        raise HTTPException(400, "этот пользователь — администратор из настроек бота; уберите его ID там")
    store = bots.manager.store(pid)
    if store.find(uid) is None:
        raise HTTPException(404, "пользователь не найден")
    user = store.set_role(uid, body.role)
    live = bots.manager.live.get(pid)
    if live and bots.manager.loop:
        asyncio.run_coroutine_threadsafe(bots.sync_commands(live.engine), bots.manager.loop)
    return user


class RefundIn(BaseModel):
    charge_id: str


@app.post("/api/projects/{pid}/bot/payments/refund")
async def bot_refund(pid: str, body: RefundIn):
    """Вернуть оплату звёздами из вкладки «Платежи»."""
    from .telegram import TelegramError
    project = _bot_project(pid)
    store = bots.manager.store(pid)
    payment = next((p for p in store.payments() if p.get("charge_id") == body.charge_id), None)
    if payment is None:
        raise HTTPException(404, "платёж не найден")
    if payment.get("currency") != "XTR":
        raise HTTPException(400, "из платформы возвращаются только Stars; оплату в валюте возвращает платёжный провайдер")
    if payment.get("refunded"):
        raise HTTPException(409, "уже возвращён")
    live = bots.manager.live.get(pid)
    if live:
        transport = live.engine.transport
    else:
        try:
            token = bots.manager.compiled_for(pid, project).token
        except (GraphError, vault.VaultError) as e:
            raise HTTPException(400, str(e)) from None
        if not token:
            raise HTTPException(400, "не задан токен бота")
        transport = bots.TelegramTransport(token)
    try:
        await transport.refund(payment["user_id"], body.charge_id)
    except TelegramError as e:
        raise HTTPException(502, f"Telegram: {e}") from None
    store.mark_refunded(body.charge_id)
    return {"ok": True}


@app.get("/api/projects/{pid}/bot/payments")
def bot_payments(pid: str):
    _bot_project(pid)
    return bots.manager.store(pid).payments()


@app.delete("/api/projects/{pid}/bot/users")
def bot_users_clear(pid: str):
    _bot_project(pid)
    bots.manager.store(pid).clear()
    return {"ok": True}


# --- агенты ---

@app.get("/api/agents")
def agents_list(request: Request):
    return hub.listing(_user(request))


@app.post("/api/agents")
def agents_create(body: AgentIn, request: Request):
    user = _user(request)
    rec, token = hub.create_agent(body.name, user["id"] if user else None)
    return {"agent": rec, "token": token}


@app.put("/api/agents/{agent_id}")
def agents_update(agent_id: str, body: AgentIn):
    try:
        return hub.update_agent(agent_id, body.name or None, body.users)
    except KeyError:
        raise HTTPException(404, "агент не найден") from None


@app.delete("/api/agents/{agent_id}")
async def agents_delete(agent_id: str):
    if agent_id == LOCAL_ID:
        raise HTTPException(400, "локальный агент нельзя удалить — его можно выключить (local_agent = false)")
    await hub.delete_agent(agent_id)
    return {"ok": True}


@app.get("/api/agents/bundle.zip")
def agents_bundle():
    return Response(agent_bundle(), media_type="application/zip",
                    headers={"Content-Disposition": 'attachment; filename="dragapi-agent.zip"'})


@app.websocket("/api/agents/ws")
async def agents_ws(websocket: WebSocket):
    await hub.websocket(websocket)


# --- вебхуки сценариев (вызов из API-проектов и откуда угодно) ---

@app.post("/hooks/{pid}/{name}")
async def scenario_hook(pid: str, name: str, request: Request, wait: bool = True, timeout: float = 60):
    """POST /hooks/<проект>/<сценарий> с Authorization: Bearer <токен вебхуков> и JSON-параметрами."""
    import hashlib
    import secrets as _secrets
    try:
        project = storage.load(pid)
    except storage.NotFound:
        raise HTTPException(404, "сценарий не найден") from None
    auto = project.get("automation") or {}
    header = request.headers.get("authorization", "")
    token = header[7:].strip() if header.lower().startswith("bearer ") else ""
    expected = auto.get("hook_token_hash")
    if project.get("kind") != "automation" or not expected or not token or \
            not _secrets.compare_digest(hashlib.sha256(token.encode()).hexdigest(), expected):
        raise HTTPException(401, "неверный токен вебхука")
    compiled = hub.enabled.get(pid)
    if compiled is None:
        raise HTTPException(409, "сценарий выключен — включите его в настройках проекта")
    scenario = compiled.scenario(name)
    if scenario is None or scenario.trigger.kind != "webhook":
        raise HTTPException(404, f"нет сценария «{name}» с запуском по HTTP")
    try:
        inputs = await request.json() if (await request.body()) else {}
    except ValueError:
        raise HTTPException(400, "тело должно быть JSON-объектом") from None
    if not isinstance(inputs, dict):
        raise HTTPException(400, "тело должно быть JSON-объектом")
    try:
        run = await hub.start_run(pid, compiled, name, inputs, "webhook", hub.enabled_by.get(pid))
    except ValueError as e:
        raise HTTPException(409, str(e)) from None
    if wait:
        try:
            await asyncio.wait_for(run.done.wait(), min(max(timeout, 1), 600))
        except asyncio.TimeoutError:
            return FastJSON({"run_id": run.id, "status": "running", "message": "сценарий ещё выполняется"},
                            status_code=202)
    return {"run_id": run.id, "status": run.status, "message": run.message, "vars": run.vars}


app.mount("/run", runner)

# Собранный фронтенд (npm run build) раздаётся тем же сервером.
_DIST = Path(__file__).resolve().parent.parent.parent / "frontend" / "dist"
if _DIST.exists():
    app.mount("/assets", StaticFiles(directory=_DIST / "assets"), name="assets")

    @app.get("/{full_path:path}", include_in_schema=False)
    def spa(full_path: str):
        return FileResponse(_DIST / "index.html")
