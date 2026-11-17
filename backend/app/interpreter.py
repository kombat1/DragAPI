"""Интерпретатор: план проекта -> живое FastAPI-приложение для превью.

Для каждого эндпоинта собирается обработчик с «настоящей» сигнатурой
(Path/Query/Body/Form/File/Security), поэтому Swagger у превью полноценный,
включая кнопку Authorize.
"""

from __future__ import annotations

import inspect
import time
from collections import deque
from typing import Any, Optional

from fastapi import Body, FastAPI, File, Form, Path, Query, Security, UploadFile
from fastapi.encoders import jsonable_encoder
from fastapi.responses import JSONResponse
from fastapi.security import APIKeyHeader, APIKeyQuery, HTTPBearer
from pydantic import create_model
from starlette.concurrency import run_in_threadpool

from . import expr, sandbox
from .graph import (
    AuthStep, Branch, Chain, CodeStep, CompiledProject, DbStep, EndpointPlan, FieldSpec, InputStep,
    LogicStep, ValidateStep, check_rule,
)
from .runtime import Database, apply_code_result, check_auth

PY_TYPES = {"str": str, "int": int, "float": float, "bool": bool, "file": UploadFile}
JSON_PARAM = "json_body_"


class StepFailure(Exception):
    def __init__(self, node_id: str, status: int, detail: dict):
        self.node_id, self.status, self.detail = node_id, status, detail


def security_dependency(step: AuthStep):
    """Схема безопасности FastAPI (общая для превью и экспорта): даёт кнопку Authorize в Swagger."""
    # имя схемы в OpenAPI: только [A-Za-z0-9._-]
    scheme_name = f"{step.scheme}_{step.name}_{step.secret_env}".replace(" ", "_")
    if step.scheme == "bearer":
        return HTTPBearer(auto_error=False, scheme_name=scheme_name)
    if step.scheme == "api_key_query":
        return APIKeyQuery(name=step.name, auto_error=False, scheme_name=scheme_name)
    return APIKeyHeader(name=step.name, auto_error=False, scheme_name=scheme_name)


def _field_param(source: str, f: FieldSpec) -> inspect.Parameter:
    pname = f"p_{f.name}"
    marker = {"query": Query, "form": Form}[source]
    if f.type == "file":
        default = File(..., alias=f.name) if f.required else File(None, alias=f.name)
        ann = UploadFile if f.required else Optional[UploadFile]
    else:
        default = marker(..., alias=f.name) if f.required else marker(f.default, alias=f.name)
        ann = PY_TYPES[f.type] if f.required else Optional[PY_TYPES[f.type]]
    return inspect.Parameter(pname, inspect.Parameter.KEYWORD_ONLY, default=default, annotation=ann)


def build_json_model(plan: EndpointPlan):
    fields = plan.fields_by_source("json")
    if not fields:
        return None
    defs = {
        f.name: (PY_TYPES[f.type], ...) if f.required else (Optional[PY_TYPES[f.type]], f.default)
        for f in fields
    }
    return create_model(f"{plan.operation_id.title().replace('_', '')}Body", **defs)


def _signature(plan: EndpointPlan) -> inspect.Signature:
    kw = inspect.Parameter.KEYWORD_ONLY
    params: list[inspect.Parameter] = []
    path_types = {f.name: f.type for f in plan.fields_by_source("path")}
    for p in plan.path_params:
        params.append(inspect.Parameter(p, kw, default=Path(...), annotation=PY_TYPES[path_types.get(p, "str")]))
    for source in ("query", "form"):
        for f in plan.fields_by_source(source):
            params.append(_field_param(source, f))
    model = build_json_model(plan)
    if model is not None:
        params.append(inspect.Parameter(JSON_PARAM, kw, default=Body(...), annotation=model))
    for a in plan.auths:
        params.append(inspect.Parameter(a.param, kw, default=Security(security_dependency(a)), annotation=Any))
    return inspect.Signature(params)


def _ms(t0: float) -> float:
    return round((time.perf_counter() - t0) * 1000, 2)


class _Run:
    """Одно выполнение запроса: контекст, трасса, доступ к БД и окружению."""

    def __init__(self, kwargs: dict, env: dict, db: Database | None, project: CompiledProject, venv_provider):
        self.kwargs, self.env, self.db = kwargs, env, db
        self.project, self.venv_provider = project, venv_provider
        self.on_network = None  # колбэк журнала firewall
        self.ctx: dict[str, Any] = {}
        self.trace: list[dict] = []
        self.extra: dict = {}  # дополнительные сведения шага для трассы (stdout кода и т.п.)

    def eval(self, node_id: str, assignment, label: str | None = None):
        try:
            return expr.evaluate(assignment.tree, self.ctx, self.env)
        except expr.ExprError as e:
            raise StepFailure(node_id, 500, {"error": f"{label or assignment.target} = {assignment.source}: {e}"})

    async def chain(self, chain: Chain) -> tuple[int, Any]:
        for step in chain.steps:
            t0 = time.perf_counter()
            self.extra = {}
            await self.step(step)
            entry = {"node_id": step.node_id, "kind": step.kind, "ms": _ms(t0), "ctx": jsonable_encoder(self.ctx)}
            self.trace.append({**entry, **self.extra})

        end = chain.end
        if isinstance(end, Branch):
            value = bool(self.eval(end.node_id, end.condition, "условие"))
            self.trace.append({"node_id": end.node_id, "kind": "condition", "ms": 0,
                               "ctx": {"условие": end.condition.source, "результат": value}})
            return await self.chain(end.then if value else end.otherwise)

        if end.body:
            payload = {a.target: self.eval(end.node_id, a) for a in end.body}
        else:
            payload = dict(self.ctx)
        self.trace.append({"node_id": end.node_id, "kind": "response", "ms": 0, "ctx": jsonable_encoder(self.ctx)})
        return end.status, payload

    async def step(self, step) -> None:
        ctx, kwargs = self.ctx, self.kwargs
        if isinstance(step, InputStep):
            body = kwargs.get(JSON_PARAM)
            for f in step.fields:
                if step.source == "path":
                    value = kwargs[f.name]
                elif step.source == "json":
                    value = getattr(body, f.name)
                else:
                    value = kwargs[f"p_{f.name}"]
                if f.type == "file" and value is not None:
                    content = await value.read()
                    value = {"filename": value.filename, "content_type": value.content_type, "size": len(content)}
                ctx[f.name] = value

        elif isinstance(step, ValidateStep):
            errors = [r.message for r in step.rules if not check_rule(r, ctx.get(r.field))]
            if errors:
                raise StepFailure(step.node_id, 422, {"errors": errors})

        elif isinstance(step, LogicStep):
            for a in step.assignments:
                ctx[a.target] = self.eval(step.node_id, a)

        elif isinstance(step, AuthStep):
            value = kwargs.get(step.param)
            if value is not None and step.scheme == "bearer":
                value = f"Bearer {value.credentials}"
            if not check_auth(step.scheme, self.env.get(step.secret_env), value):
                raise StepFailure(step.node_id, 401, {"error": "unauthorized"})

        elif isinstance(step, DbStep):
            record_id = self.eval(step.node_id, step.id_value, "id") if step.id_value else None
            values = {a.target: self.eval(step.node_id, a) for a in step.values}
            filters = {a.target: self.eval(step.node_id, a) for a in step.filters}
            db = self.db
            try:
                if step.op == "insert":
                    result = await run_in_threadpool(db.insert, step.model, values)
                elif step.op == "get":
                    result = await run_in_threadpool(db.get, step.model, record_id)
                elif step.op == "list":
                    result = await run_in_threadpool(db.list, step.model, filters, step.limit,
                                                     step.order_by, step.descending)
                elif step.op == "update":
                    result = await run_in_threadpool(db.update, step.model, record_id, values)
                else:
                    result = await run_in_threadpool(db.delete, step.model, record_id)
            except Exception as e:  # ошибка БД — показываем, в каком блоке
                raise StepFailure(step.node_id, 500, {"error": f"БД ({step.op} {step.model}): {type(e).__name__}: {e}"})
            ctx[step.target] = result

        elif isinstance(step, CodeStep):
            r = await sandbox.run_code(step.code, jsonable_encoder(ctx), dict(self.env),
                                       sandbox.Limits(step.timeout, step.memory_mb),
                                       self.project.sandbox, self.venv_provider(), self.project.firewall)
            self.extra = {"stdout": r.stdout, "sandbox": r.backend, "sandbox_ms": r.ms, "network": r.network or []}
            if r.network and self.on_network:
                self.on_network(r.network)
            if not r.ok:
                raise StepFailure(step.node_id, 500, {"error": f"Python-код: {r.error}", "stdout": r.stdout})
            try:
                ctx.update(apply_code_result(r.result, step.outputs))
            except ValueError as e:
                raise StepFailure(step.node_id, 500, {"error": f"Python-код: {e}", "stdout": r.stdout})


def _make_handler(plan: EndpointPlan, project: CompiledProject, db: Database | None, log: deque, venv_provider,
                  on_network=None):
    env = project.env_values

    async def handler(**kwargs):
        started = time.perf_counter()
        run = _Run(kwargs, env, db, project, venv_provider)
        run.on_network = on_network
        run.ctx.update({p: kwargs[p] for p in plan.path_params})
        status, payload = 500, None
        try:
            status, payload = await run.chain(plan.chain)
            return JSONResponse(jsonable_encoder(payload), status_code=status)
        except StepFailure as f:
            status = f.status
            payload = {**f.detail, "block": f.node_id}
            run.trace.append({"node_id": f.node_id, "kind": "error", "ms": 0, "error": f.detail, **run.extra})
            return JSONResponse(payload, status_code=status)
        finally:
            log.appendleft({
                "time": time.time(), "method": plan.method, "path": plan.path,
                "endpoint": plan.node_id, "status": status, "ms": _ms(started),
                "trace": run.trace, "response": jsonable_encoder(payload),
            })

    handler.__signature__ = _signature(plan)
    handler.__name__ = plan.operation_id
    return handler


def build_app(project: CompiledProject, db: Database | None = None, log: deque | None = None,
              venv_provider=lambda: None, on_network=None) -> FastAPI:
    """venv_provider() вызывается на каждый запрос: после установки библиотек перезапуск не нужен."""
    log = log if log is not None else deque(maxlen=50)
    app = FastAPI(title=project.name or "DragAPI project", version="preview")
    for plan in project.plans:
        other = {r.status: {"description": f"Ответ блока {r.node_id}"}
                 for r in plan.responses() if r.status != plan.default_status}
        if plan.auths:
            other.setdefault(401, {"description": "Нет или неверный ключ доступа"})
        app.add_api_route(
            plan.path, _make_handler(plan, project, db, log, venv_provider, on_network), methods=[plan.method],
            summary=plan.summary or f"{plan.method} {plan.path}",
            operation_id=plan.operation_id, status_code=plan.default_status, responses=other,
        )
    app.state.log = log
    return app
