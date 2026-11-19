"""Журнал запросов API: статистика по эндпоинтам в превью и в экспортированном приложении."""

import time

import pytest
from fastapi import FastAPI
from fastapi.responses import JSONResponse
from fastapi.testclient import TestClient

from app import storage
from app.main import app as platform_app
from app.reqlog import RequestLog, RequestLogMiddleware, _error_text
from tests.test_engine import EXAMPLE, exported_app


def test_stats_window_percentiles_and_totals():
    log = RequestLog(maxlen=100)
    for ms in (10, 20, 30, 40, 100):
        log.add("GET", "/a/{id}", "/a/1", 200, ms)
    log.add("GET", "/a/{id}", "/a/2", 404, 5, "нет такой записи")
    log.add("POST", "/b", "/b", 500, 7, "упал блок")
    q = log.query()
    a = next(e for e in q["endpoints"] if e["route"] == "/a/{id}")
    assert (a["count"], a["c4"], a["c5"], a["max"], a["p50"]) == (6, 1, 0, 100, 25.0)
    assert a["last_error"]["error"] == "нет такой записи"
    assert q["endpoints"][0]["route"] == "/b"          # эндпоинты с 5xx — первыми
    errs = log.query(errors=True)["items"]
    assert [i["status"] for i in errs] == [500, 404]   # новые сверху
    assert log.query(route="POST /b")["matched"] == 1
    # окно по времени: старые запросы не в статистике окна, но в счётчиках с запуска
    for i in log.items:
        i["time"] -= 3600
    log.add("GET", "/a/{id}", "/a/3", 200, 1)
    a = log.query(since=time.time() - 60)["endpoints"][0]
    assert (a["count"], a["total"], a["total_errors"]) == (1, 7, 1)
    log.clear()
    assert log.query()["endpoints"] == []


def test_ring_buffer_keeps_last():
    log = RequestLog(maxlen=3)
    for i in range(5):
        log.add("GET", "/x", f"/x?{i}", 200, 1)
    q = log.query()
    assert q["kept"] == 3 and q["capacity"] == 3 and q["endpoints"][0]["total"] == 5


@pytest.mark.parametrize("body, text", [
    (b'{"error": "boom"}', "boom"),
    (b'{"errors": ["a", "b"], "block": "x"}', "a; b"),
    (b'{"detail": [{"loc": ["body", "price"], "msg": "Field required"}]}', "price: Field required"),
    (b"plain", "plain"),
])
def test_error_text(body, text):
    assert _error_text(body, 400) == text


def test_middleware_routes_errors_and_skips_docs():
    app = FastAPI()

    @app.get("/items/{item_id}")
    def item(item_id: int):
        if item_id == 0:
            return JSONResponse({"error": "сломалось"}, status_code=500)
        return {"id": item_id}

    @app.get("/crash")
    def crash():
        raise RuntimeError("x")

    log = RequestLog()
    c = TestClient(RequestLogMiddleware(app, log), raise_server_exceptions=False)
    c.get("/items/1")
    c.get("/items/0")
    c.get("/items/abc")          # 422 от FastAPI
    c.get("/missing")
    c.get("/crash")
    c.get("/docs")
    c.get("/openapi.json")
    q = log.query()
    by = {e["route"]: e for e in q["endpoints"]}
    assert set(by) == {"/items/{item_id}", "(нет маршрута)", "/crash"}
    assert (by["/items/{item_id}"]["count"], by["/items/{item_id}"]["c4"], by["/items/{item_id}"]["c5"]) == (3, 1, 1)
    assert by["/crash"]["c5"] == 1
    paths = [i["path"] for i in q["items"]]
    assert "/items/abc" in paths and "/docs" not in paths


def test_preview_journal_via_platform(tmp_path, monkeypatch):
    monkeypatch.setattr(storage, "DATA_DIR", tmp_path / "projects")
    monkeypatch.setattr(storage, "DB_DIR", tmp_path / "db")
    c = TestClient(platform_app, headers={"X-DragAPI": "1"})
    pid = c.post("/api/projects", json={"name": "Журнал"}).json()["id"]
    c.put(f"/api/projects/{pid}", json={k: EXAMPLE[k] for k in ("name", "nodes", "edges", "models", "env")})
    assert c.get(f"/api/projects/{pid}/requests").json()["endpoints"] == []
    c.post(f"/api/projects/{pid}/run")
    c.get(f"/run/{pid}/hello")
    c.get(f"/run/{pid}/orders/42")
    c.post(f"/run/{pid}/orders", json={"item": "x", "price": -1})
    c.get(f"/run/{pid}/docs")
    q = c.get(f"/api/projects/{pid}/requests").json()
    assert q["source"] == "preview" and q["running"]
    by = {f"{e['method']} {e['route']}": e for e in q["endpoints"]}
    assert set(by) == {"GET /hello", "GET /orders/{order_id}", "POST /orders"}
    assert by["GET /orders/{order_id}"]["last_error"]["error"] == "Заказ 42 не найден"
    assert by["POST /orders"]["c4"] == 1
    assert c.get(f"/api/projects/{pid}/requests", params={"errors": True}).json()["matched"] == 2
    # тестовые запросы из редактора в журнал не попадают
    graph = c.get(f"/api/projects/{pid}").json()
    c.post(f"/api/projects/{pid}/test-request", json={"graph": graph, "endpoint": "hello", "request": {}})
    assert c.get(f"/api/projects/{pid}/requests").json()["kept"] == 3
    assert c.delete(f"/api/projects/{pid}/requests").json() == {"ok": True}
    assert c.get(f"/api/projects/{pid}/requests").json()["kept"] == 0
    r = c.get(f"/api/projects/{pid}/requests", params={"source": "deploy"})
    assert r.status_code == 409 and "Деплой" in r.json()["detail"]


def test_exported_app_journal_needs_token(tmp_path, monkeypatch):
    monkeypatch.setenv("DRAGAPI_LOG_TOKEN", "t0k3n")
    c = TestClient(exported_app(EXAMPLE, tmp_path, monkeypatch))
    c.get("/hello")
    c.get("/orders/42")
    assert c.get("/_dragapi/requests").status_code == 404
    assert c.get("/_dragapi/requests", headers={"X-DragAPI-Log-Token": "wrong"}).status_code == 404
    q = c.get("/_dragapi/requests", headers={"X-DragAPI-Log-Token": "t0k3n"}).json()
    assert {e["route"] for e in q["endpoints"]} == {"/hello", "/orders/{order_id}"}
    assert "/_dragapi/requests" not in c.get("/openapi.json").text
    monkeypatch.delenv("DRAGAPI_LOG_TOKEN")
    assert c.get("/_dragapi/requests", headers={"X-DragAPI-Log-Token": ""}).status_code == 404
