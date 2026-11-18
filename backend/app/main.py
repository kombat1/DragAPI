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

from fastapi import FastAPI, HTTPException, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse, JSONResponse as FastJSON, Response
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel
from starlette.responses import JSONResponse
from starlette.routing import Mount, Router

from . import auth, codegen, sandbox, storage, testing, vault
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
    yield


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


def _load(pid: str) -> dict:
    try:
        return storage.load(pid)
    except storage.NotFound:
        raise HTTPException(404, "проект не найден") from None


def _compile(project: dict) -> CompiledProject:
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
    project = storage.create(body.name.strip() or "Новый проект")
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
    return {**project, "env": vault.mask_env(project.get("env")), "running": pid in runner.apps, "my_role": role}


@app.put("/api/projects/{pid}")
def save_project(pid: str, body: GraphIn):
    _load(pid)
    saved = storage.save(pid, body.model_dump())
    return {**saved, "env": vault.mask_env(saved.get("env"))}


@app.delete("/api/projects/{pid}")
async def delete_project(pid: str):
    if docker_available():
        await deployments.stop(pid, remove_data=True)
    runner.unmount(pid)
    envs.remove(pid)
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
    saved = storage.save(pid, old)
    return {**saved, "env": vault.mask_env(saved.get("env"))}


@app.post("/api/validate")
def validate(body: GraphIn, project_id: str | None = None):
    """Проверка графа без сохранения — фронтенд вызывает её при каждом изменении."""
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


app.mount("/run", runner)

# Собранный фронтенд (npm run build) раздаётся тем же сервером.
_DIST = Path(__file__).resolve().parent.parent.parent / "frontend" / "dist"
if _DIST.exists():
    app.mount("/assets", StaticFiles(directory=_DIST / "assets"), name="assets")

    @app.get("/{full_path:path}", include_in_schema=False)
    def spa(full_path: str):
        return FileResponse(_DIST / "index.html")
