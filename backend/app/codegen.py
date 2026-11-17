"""Генератор кода: план проекта -> самостоятельное FastAPI-приложение.

Экспортированный main.py не зависит от DragAPI: его можно положить в репозиторий,
править руками и деплоить как обычный сервис. Работа с БД и проверка ключей берутся
из app/runtime.py дословно — та же реализация, что и в превью.
"""

from __future__ import annotations

import inspect

from . import runtime
from .expr import FUNCTIONS_SOURCE, to_python
from .graph import (
    AuthStep, Branch, Chain, CodeStep, CompiledProject, DbStep, EndpointPlan, FieldSpec, InputStep,
    LogicStep, Rule, ValidateStep, iter_steps,
)
from .interpreter import JSON_PARAM

_TYPE = {"str": "str", "int": "int", "float": "float", "bool": "bool", "file": "UploadFile"}


def _ann(f: FieldSpec) -> str:
    t = _TYPE[f.type]
    return t if f.required else f"Optional[{t}]"


def _class_name(plan: EndpointPlan) -> str:
    return plan.operation_id.title().replace("_", "") + "Body"


def _security_var(plan: EndpointPlan, step: AuthStep) -> str:
    return f"SECURITY_{plan.operation_id.upper()}_{step.param.split('_')[-1]}"


def _rule_check(rule: Rule) -> tuple[str, str]:
    v = f"ctx[{rule.field!r}]"
    cond = {
        "min": f"{v} >= {rule.value!r}",
        "max": f"{v} <= {rule.value!r}",
        "min_len": f"len({v}) >= {rule.value!r}",
        "max_len": f"len({v}) <= {rule.value!r}",
        "regex": f"re.fullmatch({rule.value!r}, str({v}))",
        "in": f"str({v}) in {rule.value!r}",
    }[rule.op]
    return f"if ctx.get({rule.field!r}) is not None and not ({cond}):", f"errors.append({rule.message!r})"


def _dict_literal(assignments) -> str:
    return "{" + ", ".join(f"{a.target!r}: {to_python(a.tree)}" for a in assignments) + "}"


def _emit_step(step, out: list[str], pad: str, plan: EndpointPlan) -> None:
    if isinstance(step, InputStep):
        out.append(f"{pad}# Входные данные ({step.source}) — блок {step.node_id}")
        for f in step.fields:
            if step.source == "path":
                continue
            src = f"{JSON_PARAM}.{f.name}" if step.source == "json" else f"p_{f.name}"
            if f.type == "file":
                out.append(f"{pad}ctx[{f.name!r}] = await _file_info({src})")
            else:
                out.append(f"{pad}ctx[{f.name!r}] = {src}")

    elif isinstance(step, ValidateStep):
        if not step.rules:
            return
        out.append(f"{pad}# Валидация — блок {step.node_id}")
        out.append(f"{pad}errors: list[str] = []")
        for r in step.rules:
            cond, action = _rule_check(r)
            out += [f"{pad}{cond}", f"{pad}    {action}"]
        out.append(f"{pad}if errors:")
        out.append(f"{pad}    return JSONResponse({{'errors': errors, 'block': {step.node_id!r}}}, status_code=422)")

    elif isinstance(step, LogicStep):
        out.append(f"{pad}# Логика — блок {step.node_id}")
        for a in step.assignments:
            out.append(f"{pad}ctx[{a.target!r}] = {to_python(a.tree)}")

    elif isinstance(step, AuthStep):
        out.append(f"{pad}# Авторизация ({step.scheme}) — блок {step.node_id}")
        value = f"(f'Bearer {{{step.param}.credentials}}' if {step.param} else None)" if step.scheme == "bearer" \
            else step.param
        out.append(f"{pad}if not check_auth({step.scheme!r}, ENV.get({step.secret_env!r}), {value}):")
        out.append(f"{pad}    return JSONResponse({{'error': 'unauthorized', 'block': {step.node_id!r}}}, status_code=401)")

    elif isinstance(step, DbStep):
        out.append(f"{pad}# База данных: {step.op} {step.model} — блок {step.node_id}")
        record_id = to_python(step.id_value.tree) if step.id_value else None
        filters = "[" + ", ".join(f"({f.field!r}, {f.op!r}, {to_python(f.value.tree)})" for f in step.filters) + "]"
        offset = to_python(step.offset.tree) if step.offset else "0"
        args = {
            "insert": f"{_dict_literal(step.values)}",
            "get": f"{record_id}" + (f", {step.include!r}" if step.include else ""),
            "list": f"{filters}, {to_python(step.limit_value.tree) if step.limit_value else repr(step.limit)}, "
                    f"{step.order_by!r}, {step.descending!r}, {offset}, {step.include!r}",
            "update": f"{record_id}, {_dict_literal(step.values)}",
            "delete": f"{record_id}",
        }[step.op]
        out.append(f"{pad}ctx[{step.target!r}] = await run_in_threadpool(db.{step.op}, {step.model!r}, {args})")
        if step.op == "list" and step.count_target:
            out.append(f"{pad}ctx[{step.count_target!r}] = await run_in_threadpool(db.count, {step.model!r}, {filters})")

    elif isinstance(step, CodeStep):
        out.append(f"{pad}# Python-код — блок {step.node_id} (blocks/{step.module}.py)")
        out.append(f"{pad}result = await run_in_threadpool({step.module}, jsonable_encoder(ctx), dict(ENV))")
        out.append(f"{pad}ctx.update(apply_code_result(result, {step.outputs!r}))")


def _emit_chain(chain: Chain, out: list[str], pad: str, plan: EndpointPlan) -> None:
    for step in chain.steps:
        _emit_step(step, out, pad, plan)
    end = chain.end
    if isinstance(end, Branch):
        out.append(f"{pad}# Условие — блок {end.node_id}")
        out.append(f"{pad}if {to_python(end.condition.tree)}:")
        _emit_chain(end.then, out, pad + "    ", plan)
        out.append(f"{pad}else:")
        _emit_chain(end.otherwise, out, pad + "    ", plan)
        return
    out.append(f"{pad}# Ответ — блок {end.node_id}")
    payload = _dict_literal(end.body) if end.body else "ctx"
    out.append(f"{pad}return JSONResponse(jsonable_encoder({payload}), status_code={end.status})")


def _endpoint(plan: EndpointPlan) -> str:
    out: list[str] = []
    path_types = {f.name: f.type for f in plan.fields_by_source("path")}
    params = [f"{p}: {_TYPE[path_types.get(p, 'str')]} = Path(...)" for p in plan.path_params]
    for source, marker in (("query", "Query"), ("form", "Form")):
        for f in plan.fields_by_source(source):
            m = "File" if f.type == "file" else marker
            default = "..." if f.required else repr(f.default)
            params.append(f"p_{f.name}: {_ann(f)} = {m}({default}, alias={f.name!r})")
    if plan.fields_by_source("json"):
        params.append(f"{JSON_PARAM}: {_class_name(plan)} = Body(...)")
    for a in plan.auths:
        params.append(f"{a.param}: Any = Security({_security_var(plan, a)})")

    responses = {r.status: f"Ответ блока {r.node_id}" for r in plan.responses() if r.status != plan.default_status}
    if plan.auths:
        responses.setdefault(401, "Нет или неверный ключ доступа")
    responses_src = "{" + ", ".join(f"{k}: {{'description': {v!r}}}" for k, v in responses.items()) + "}"
    summary = plan.summary or f"{plan.method} {plan.path}"
    out.append(f"@app.{plan.method.lower()}({plan.path!r}, summary={summary!r}, "
               f"status_code={plan.default_status}, responses={responses_src})")
    out.append(f"async def {plan.operation_id}({', '.join(params)}):")
    ctx_init = ", ".join(f"{p!r}: {p}" for p in plan.path_params)
    out.append(f"    ctx: dict = {{{ctx_init}}}")
    _emit_chain(plan.chain, out, "    ", plan)
    return "\n".join(out)


def _security_definitions(plans: list[EndpointPlan]) -> list[str]:
    out = []
    for plan in plans:
        for a in plan.auths:
            scheme_name = f"{a.scheme}_{a.name}_{a.secret_env}".replace(" ", "_")
            if a.scheme == "bearer":
                ctor = f"HTTPBearer(auto_error=False, scheme_name={scheme_name!r})"
            elif a.scheme == "api_key_query":
                ctor = f"APIKeyQuery(name={a.name!r}, auto_error=False, scheme_name={scheme_name!r})"
            else:
                ctor = f"APIKeyHeader(name={a.name!r}, auto_error=False, scheme_name={scheme_name!r})"
            out.append(f"{_security_var(plan, a)} = {ctor}")
    return out


def code_steps(project: CompiledProject) -> list[CodeStep]:
    seen, out = set(), []
    for plan in project.plans:
        for step in iter_steps(plan.chain):
            if isinstance(step, CodeStep) and step.module not in seen:
                seen.add(step.module)
                out.append(step)
    return out


def needs_runtime(project: CompiledProject) -> bool:
    return (project.uses_db or any(p.auths for p in project.plans) or bool(code_steps(project))
            or project.firewall.ingress.enabled)


def generate(project: CompiledProject) -> str:
    title = project.name or "DragAPI project"
    parts = [
        f'"""Сгенерировано DragAPI из проекта «{title}».\n\n'
        f"Запуск: uvicorn main:app --reload\n"
        f"Настройки — через переменные окружения (см. .env.example).\n\"\"\"",
        "",
        "import os",
        "import re",
        "from typing import Any, Optional",
        "",
        "from fastapi import Body, FastAPI, File, Form, Path, Query, Security, UploadFile",
        "from fastapi.encoders import jsonable_encoder",
        "from fastapi.responses import JSONResponse",
        "from fastapi.security import APIKeyHeader, APIKeyQuery, HTTPBearer",
        "from pydantic import BaseModel",
        "from starlette.concurrency import run_in_threadpool",
        "",
    ]
    if needs_runtime(project):
        parts += ["", inspect.getsource(runtime).strip(), "", ""]
        parts.append("# --- конец runtime ---")

    parts += ["", "# Переменные окружения проекта. Секреты не попадают в код — задайте их в окружении."]
    parts.append("ENV = {")
    for e in project.env:
        default = "''" if e.secret else repr(e.value)
        comment = "  # секрет" if e.secret else ""
        parts.append(f"    {e.key!r}: os.environ.get({e.key!r}, {default}),{comment}")
    parts.append("}")

    if project.uses_db:
        parts += ["", "# Модели: id полей позволяют переименовывать поля без потери данных (миграции при старте)",
                  f"MODELS = {[m.as_dict() for m in project.models]!r}",
                  "db = Database(os.environ.get('DATABASE_URL', 'sqlite:///./app.db'), MODELS)"]

    parts += ["", f"app = FastAPI(title={title!r})"]
    if project.uses_db:
        parts.append("install_db_error_handler(app)  # нарушение связей и т.п. -> 409")
    ingress = project.firewall.ingress
    if ingress.enabled:
        parts += ["", "# Firewall входящих запросов: первое совпавшее правило; иначе — значение по умолчанию.",
                  "# За обратным прокси включите trust_proxy, иначе все клиенты будут иметь IP прокси.",
                  f"app.add_middleware(IngressFirewall, rules={ingress.runtime_rules()!r}, "
                  f"default={ingress.default!r}, trust_proxy={ingress.trust_proxy!r})"]
    parts += ["", "", "# --- функции, доступные в выражениях ---",
              FUNCTIONS_SOURCE, "", "",
              "async def _file_info(f):",
              "    if f is None:",
              "        return None",
              "    content = await f.read()",
              "    return {'filename': f.filename, 'content_type': f.content_type, 'size': len(content)}"]

    blocks = code_steps(project)
    if blocks:
        parts += ["", "", "# --- блоки «Python-код»: ваш код лежит в blocks/*.py и выполняется в этом процессе ---"]
        parts += [f"from blocks.{b.module} import run as {b.module}  # noqa: E402" for b in blocks]

    security = _security_definitions(project.plans)
    if security:
        parts += ["", ""] + security

    for plan in project.plans:
        fields = plan.fields_by_source("json")
        if fields:
            parts += ["", "", f"class {_class_name(plan)}(BaseModel):"]
            for f in fields:
                default = "" if f.required else f" = {f.default!r}"
                parts.append(f"    {f.name}: {_ann(f)}{default}")
    for plan in project.plans:
        parts += ["", "", _endpoint(plan)]
    return "\n".join(parts) + "\n"


def requirements(project: CompiledProject) -> str:
    req = "fastapi>=0.110\nuvicorn[standard]>=0.29\npython-multipart>=0.0.9\n"
    if needs_runtime(project):
        req += "sqlalchemy>=2.0\n"
    if project.packages:
        req += "# библиотеки для блоков «Python-код»\n" + "".join(p + "\n" for p in project.packages)
    return req


def env_example(project: CompiledProject) -> str:
    lines = ["# Переменные окружения проекта"]
    if project.uses_db:
        lines.append("DATABASE_URL=sqlite:///./app.db")
    for e in project.env:
        if e.secret:
            lines.append(f"# {e.key} — секрет, задайте значение")
        lines.append(f"{e.key}=" + ("" if e.secret else e.value))
    return "\n".join(lines) + "\n"


def generate_tests(project: CompiledProject) -> str:
    """tests/test_api.py: тест-кейсы проекта для pytest (та же логика сравнения, что в редакторе)."""
    import json as _json

    from . import testing

    plans = {p.node_id: p for p in project.plans}
    cases = []
    for t in project.tests:
        plan = plans[t["endpoint"]]
        cases.append({"name": t.get("name") or t.get("id"), "method": plan.method, "path": plan.path,
                      "request": t.get("request") or {}, "expect": t.get("expect") or {}})
    cases_src = _json.dumps(cases, ensure_ascii=False, indent=4)
    return f'''"""Автотесты, сохранённые в DragAPI. Запуск из корня проекта: pytest

Тесты идут по порядку и делят одну временную базу (могут зависеть друг от друга).
Значения вида {{{{env.KEY}}}} берутся из переменных окружения — задайте секреты так же, как для main.py.
"""

import json
import os
import re
import tempfile
from urllib.parse import quote

import pytest

os.environ.setdefault("DATABASE_URL", "sqlite:///" + os.path.join(tempfile.mkdtemp(), "test.db"))

from fastapi.testclient import TestClient  # noqa: E402

import main  # noqa: E402

TEMPLATE_RE = re.compile(r"\\{{\\{{\\s*env\\.([A-Za-z_][A-Za-z0-9_]*)\\s*\\}}\\}}")


class TemplateError(ValueError):
    pass


{inspect.getsource(testing.render_templates).strip()}


{inspect.getsource(testing.match_subset).strip()}


CASES = {cases_src}


async def _local(scope, receive, send):
    # запросы идут как будто с 127.0.0.1 — для входящего firewall проекта
    await main.app({{**scope, "client": ("127.0.0.1", 50000)}}, receive, send)


@pytest.fixture(scope="module")
def client():
    return TestClient(_local)


@pytest.mark.parametrize("case", CASES, ids=[c["name"] for c in CASES])
def test_case(client, case):
    request = render_templates(case["request"], dict(os.environ))
    path_params = request.get("path") or {{}}
    url = re.sub(r"{{([A-Za-z_][A-Za-z0-9_]*)}}", lambda m: quote(str(path_params[m.group(1)]), safe=""), case["path"])
    kwargs = {{"params": request.get("query") or {{}}, "headers": request.get("headers") or {{}}}}
    if request.get("body_type") == "json" and str(request.get("body") or "").strip():
        kwargs["json"] = json.loads(request["body"]) if isinstance(request["body"], str) else request["body"]
    elif request.get("body_type") == "form":
        kwargs["data"] = request.get("form") or {{}}
    response = client.request(case["method"], url, **kwargs)
    expect = case["expect"]
    if str(expect.get("status") or "").strip():
        assert response.status_code == int(expect["status"]), response.text
    body = expect.get("body")
    if isinstance(body, str):
        body = json.loads(body) if body.strip() else None
    if body is not None:
        problems = match_subset(body, response.json())
        assert not problems, "\\n".join(problems)
'''


def generate_files(project: CompiledProject) -> dict[str, str]:
    """Все файлы экспортируемого проекта: путь -> содержимое."""
    files = {
        "main.py": generate(project),
        "requirements.txt": requirements(project),
        ".env.example": env_example(project),
    }
    if project.tests:
        files["tests/test_api.py"] = generate_tests(project)
        files["tests/conftest.py"] = ("import os\nimport sys\n\n"
                                      "sys.path.insert(0, os.path.dirname(os.path.dirname(__file__)))\n")
        files["requirements-dev.txt"] = "-r requirements.txt\npytest>=8\nhttpx>=0.27\n"
    blocks = code_steps(project)
    if blocks:
        files["blocks/__init__.py"] = ""
        for b in blocks:
            files[f"blocks/{b.module}.py"] = (
                f"# Блок «Python-код» {b.node_id}. Выходные поля: {', '.join(b.outputs) or 'нет'}.\n"
                f"# В DragAPI он выполнялся в песочнице; здесь — обычный модуль вашего сервиса.\n\n"
                + b.code.rstrip() + "\n"
            )
    return files
