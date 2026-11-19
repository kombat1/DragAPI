# --- DragAPI: журнал запросов API ---
# Модуль самодостаточен (только стандартная библиотека): генератор кода вставляет его в экспортированный
# main.py дословно, поэтому превью в платформе и развёрнутый контейнер считают статистику одинаково.

import hmac
import json
import os
import time
from collections import deque

REQLOG_SKIP = ("/docs", "/redoc", "/openapi.json", "/_dragapi/", "/favicon.ico")


def _percentile(sorted_values, q):
    if not sorted_values:
        return None
    k = (len(sorted_values) - 1) * q
    lo = int(k)
    hi = min(lo + 1, len(sorted_values) - 1)
    return round(sorted_values[lo] + (sorted_values[hi] - sorted_values[lo]) * (k - lo), 1)


class RequestLog:
    """Последние запросы (кольцевой буфер) и счётчики по эндпоинтам с момента запуска."""

    def __init__(self, maxlen=5000):
        self.items = deque(maxlen=maxlen)
        self.started = time.time()
        self.totals = {}   # "GET /orders/{id}" -> {count, c4, c5}

    def add(self, method, route, path, status, ms, error=None, client=None):
        key = f"{method} {route}"
        t = self.totals.setdefault(key, {"count": 0, "c4": 0, "c5": 0})
        t["count"] += 1
        if 400 <= status < 500:
            t["c4"] += 1
        elif status >= 500:
            t["c5"] += 1
        self.items.append({"time": time.time(), "method": method, "route": route, "path": path,
                           "status": status, "ms": round(ms, 1), "error": error, "client": client})

    def clear(self):
        self.items.clear()
        self.totals.clear()
        self.started = time.time()

    def query(self, since=None, errors=False, route=None, limit=200):
        """Статистика по эндпоинтам за окно (since — unix-время) и последние запросы под фильтр."""
        window = [i for i in self.items if since is None or i["time"] >= since]
        stats = {}
        for i in window:
            key = f"{i['method']} {i['route']}"
            s = stats.setdefault(key, {"method": i["method"], "route": i["route"], "count": 0, "c4": 0, "c5": 0,
                                       "ms": [], "last": 0, "last_error": None})
            s["count"] += 1
            s["ms"].append(i["ms"])
            s["last"] = max(s["last"], i["time"])
            if 400 <= i["status"] < 500:
                s["c4"] += 1
            elif i["status"] >= 500:
                s["c5"] += 1
            if i["status"] >= 400:
                s["last_error"] = {"time": i["time"], "status": i["status"], "error": i["error"]}
        endpoints = []
        for key, s in stats.items():
            ms = sorted(s.pop("ms"))
            total = self.totals.get(key, {})
            endpoints.append({**s, "avg": round(sum(ms) / len(ms), 1), "p50": _percentile(ms, 0.5),
                              "p95": _percentile(ms, 0.95), "max": ms[-1],
                              "total": total.get("count", 0), "total_errors": total.get("c4", 0) + total.get("c5", 0)})
        endpoints.sort(key=lambda e: (-e["c5"], -e["count"]))
        picked = [i for i in reversed(window)
                  if (not errors or i["status"] >= 400) and (route is None or f"{i['method']} {i['route']}" == route)]
        limit = max(1, min(int(limit or 200), 1000))
        return {"started": self.started, "now": time.time(), "kept": len(self.items), "capacity": self.items.maxlen,
                "endpoints": endpoints, "items": picked[:limit], "matched": len(picked)}


def _error_text(body, status):
    try:
        data = json.loads(body.decode("utf-8", "replace"))
    except ValueError:
        return body.decode("utf-8", "replace")[:300] or None
    if isinstance(data, dict):
        for key in ("error", "errors", "detail", "message"):
            err = data.get(key)
            if err:
                return _short(err)[:300]
    return json.dumps(data, ensure_ascii=False)[:300]


def _short(err):
    """Текст ошибки: строка как есть, список — через «; », ошибки FastAPI (422) — «поле: сообщение»."""
    if isinstance(err, str):
        return err
    if isinstance(err, list):
        return "; ".join(_short(e) for e in err)
    if isinstance(err, dict) and "msg" in err:
        loc = [str(x) for x in err.get("loc") or [] if x not in ("body", "query", "path", "header")]
        return (".".join(loc) + ": " if loc else "") + str(err["msg"])
    return json.dumps(err, ensure_ascii=False)


class RequestLogMiddleware:
    """ASGI-прослойка: время ответа и статус каждого запроса; у ошибок — текст из тела ответа."""

    def __init__(self, app, log):
        self.app = app
        self.log = log

    async def __call__(self, scope, receive, send):
        if scope["type"] != "http":
            return await self.app(scope, receive, send)
        root = scope.get("root_path", "")
        path = scope.get("path", "")
        rel = path[len(root):] if root and path.startswith(root) else path
        if rel.startswith(REQLOG_SKIP) or rel in ("/docs", "/redoc", "/openapi.json"):
            return await self.app(scope, receive, send)
        started = time.perf_counter()
        state = {"status": 500, "body": b""}

        async def wrapped(message):
            if message["type"] == "http.response.start":
                state["status"] = message["status"]
            elif message["type"] == "http.response.body" and state["status"] >= 400 and len(state["body"]) < 2000:
                state["body"] += message.get("body", b"")[:2000]
            await send(message)

        try:
            await self.app(scope, receive, wrapped)
        finally:
            route = scope.get("route")
            template = getattr(route, "path", None) or ("(нет маршрута)" if state["status"] == 404 else rel)
            client = (scope.get("client") or [None])[0]
            error = _error_text(state["body"], state["status"]) if state["status"] >= 400 else None
            self.log.add(scope.get("method", "?"), template, rel, state["status"],
                         (time.perf_counter() - started) * 1000, error, client)


def reqlog_allowed(header_value):
    """Доступ к журналу развёрнутого приложения: заголовок X-DragAPI-Log-Token = DRAGAPI_LOG_TOKEN."""
    token = os.environ.get("DRAGAPI_LOG_TOKEN", "")
    return bool(token) and hmac.compare_digest(str(header_value or "").encode(), token.encode())
