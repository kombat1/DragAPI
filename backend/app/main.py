"""DragAPI — сервер платформы.

  /api/...           управление проектами, проверка графа, запуск, экспорт
  /run/<id>/...      «живое» API запущенного проекта (+ /run/<id>/docs — Swagger)
"""

from __future__ import annotations

import io
import zipfile
from collections import deque
from pathlib import Path

import time

from fastapi import FastAPI, HTTPException, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse, Response
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel
from starlette.responses import JSONResponse
from starlette.routing import Mount, Router

from . import codegen, sandbox, storage
from .pyenv import envs, validate_packages
from .blocks import catalog
from .graph import CompiledProject, GraphError, compile_project
from .interpreter import build_app
from .firewall import FirewallSettings
from .runtime import Database, IngressFirewall

app = FastAPI(title="DragAPI", version="0.2.0")
app.add_middleware(CORSMiddleware, allow_origins=["*"], allow_methods=["*"], allow_headers=["*"])


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
    viewport: dict | None = None


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
    except Exception as e:
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
    result = await sandbox.run_code(body.code, body.data, body.env, limits, settings, envs.venv(pid),
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
def projects():
    return storage.list_projects()


@app.post("/api/projects")
def create_project(body: CreateIn):
    return storage.create(body.name.strip() or "Новый проект")


@app.get("/api/projects/{pid}")
def get_project(pid: str):
    project = _load(pid)
    return {**project, "running": pid in runner.apps}


@app.put("/api/projects/{pid}")
def save_project(pid: str, body: GraphIn):
    _load(pid)
    return storage.save(pid, body.model_dump())


@app.delete("/api/projects/{pid}")
def delete_project(pid: str):
    runner.unmount(pid)
    envs.remove(pid)
    try:
        storage.delete(pid)
    except storage.NotFound:
        raise HTTPException(404, "проект не найден") from None
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
    return storage.save(pid, old)


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
    project = _load(pid)
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
    }


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
    db.clear(model)
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
        for path, content in files.items():
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
