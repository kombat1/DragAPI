"""Тестовые запросы и тест-кейсы проекта.

Функции render_templates и match_subset вставляются в экспортированные тесты дословно
(inspect.getsource), поэтому «Прогнать все» в редакторе и pytest в экспорте проверяют одинаково.
"""

from __future__ import annotations

import json
import re
import time
from collections import deque
from urllib.parse import quote

import httpx

TEMPLATE_RE = re.compile(r"\{\{\s*env\.([A-Za-z_][A-Za-z0-9_]*)\s*\}\}")


class TemplateError(ValueError):
    pass


def render_templates(value, env):
    """{{env.KEY}} в строках (рекурсивно в dict/list) -> значение переменной окружения проекта."""
    if isinstance(value, str):
        def sub(m):
            if m.group(1) not in env:
                raise TemplateError(f"переменная окружения {m.group(1)} не объявлена")
            return str(env[m.group(1)])
        return TEMPLATE_RE.sub(sub, value)
    if isinstance(value, dict):
        return {k: render_templates(v, env) for k, v in value.items()}
    if isinstance(value, list):
        return [render_templates(v, env) for v in value]
    return value


def match_subset(expected, actual, path="$"):
    """Расхождения между ожидаемым и фактическим. dict — по подмножеству ключей, остальное — точно."""
    if isinstance(expected, dict):
        if not isinstance(actual, dict):
            return [f"{path}: ожидался объект, получено {json.dumps(actual, ensure_ascii=False)[:80]}"]
        problems = []
        for key, value in expected.items():
            if key not in actual:
                problems.append(f"{path}.{key}: нет в ответе")
            else:
                problems += match_subset(value, actual[key], f"{path}.{key}")
        return problems
    if isinstance(expected, list):
        if not isinstance(actual, list) or len(actual) != len(expected):
            got = f"{len(actual)} элементов" if isinstance(actual, list) else json.dumps(actual, ensure_ascii=False)[:80]
            return [f"{path}: ожидался список из {len(expected)} элементов, получено {got}"]
        problems = []
        for i, (e, a) in enumerate(zip(expected, actual)):
            problems += match_subset(e, a, f"{path}[{i}]")
        return problems
    if expected != actual:
        return [f"{path}: ожидалось {json.dumps(expected, ensure_ascii=False)}, "
                f"получено {json.dumps(actual, ensure_ascii=False)[:120]}"]
    return []


def build_url(path_template: str, path_params: dict) -> str:
    def sub(m):
        name = m.group(1)
        if name not in path_params or path_params[name] in ("", None):
            raise TemplateError(f"не задан параметр пути {name}")
        return quote(str(path_params[name]), safe="")
    return re.sub(r"{([A-Za-z_][A-Za-z0-9_]*)}", sub, path_template)


async def send(app, plan, request: dict, env: dict, log: deque) -> dict:
    """Выполнить запрос к приложению в памяти. request: {path, query, headers, body_type, body, form}."""
    request = render_templates(request, env)
    url = build_url(plan.path, request.get("path") or {})
    kwargs: dict = {
        "params": {k: v for k, v in (request.get("query") or {}).items() if k},
        "headers": {k: v for k, v in (request.get("headers") or {}).items() if k},
    }
    body_type = request.get("body_type") or "none"
    if body_type == "json":
        raw = request.get("body")
        if isinstance(raw, str):
            if raw.strip():
                try:
                    kwargs["content"] = json.dumps(json.loads(raw), ensure_ascii=False).encode()
                except json.JSONDecodeError as e:
                    raise TemplateError(f"тело запроса — некорректный JSON: {e}") from None
                kwargs["headers"].setdefault("content-type", "application/json")
        elif raw is not None:
            kwargs["json"] = raw
    elif body_type == "form":
        kwargs["data"] = {k: v for k, v in (request.get("form") or {}).items() if k}

    log.clear()
    transport = httpx.ASGITransport(app=app, client=("127.0.0.1", 50000), raise_app_exceptions=False)
    started = time.perf_counter()
    async with httpx.AsyncClient(transport=transport, base_url="http://preview") as client:
        response = await client.request(plan.method, url, **kwargs)
    ms = round((time.perf_counter() - started) * 1000, 1)
    try:
        body = response.json()
        body_kind = "json"
    except ValueError:
        body, body_kind = response.text, "text"
    entry = log[0] if log else None
    return {
        "status": response.status_code, "ms": ms, "url": url, "method": plan.method,
        "headers": {k: v for k, v in response.headers.items() if k in ("content-type", "content-length")},
        "body": body, "body_kind": body_kind,
        "endpoint": plan.node_id,
        "trace": entry["trace"] if entry else [],
        "rejected_before_chain": entry is None,
    }


def check_expectation(result: dict, expect: dict) -> list[str]:
    problems = []
    status = expect.get("status")
    if status not in (None, "") and int(status) != result["status"]:
        problems.append(f"статус: ожидался {status}, получен {result['status']}")
    body = expect.get("body")
    if isinstance(body, str) and body.strip():
        try:
            body = json.loads(body)
        except json.JSONDecodeError as e:
            return problems + [f"ожидаемое тело — некорректный JSON: {e}"]
    elif isinstance(body, str):
        body = None
    if body is not None:
        problems += match_subset(body, result["body"])
    return problems
