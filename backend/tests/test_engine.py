import copy
import os
import importlib.util
import re
import json
import sys
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from app import codegen, expr, sandbox, storage, vault
from app.pyenv import EnvManager, validate_packages
from app.sandbox import SandboxSettings
from app.firewall import EgressPolicy, FirewallSettings, parse_rule
from app.graph import GraphError, compile_project
from app.interpreter import build_app
from app.main import app as platform_app
from app.runtime import Database

EXAMPLE = json.loads((Path(__file__).parent.parent / "examples/example-shop.json").read_text("utf-8"))
KEY = {"X-API-Key": "dev-secret"}


def exported_app(graph, tmp_path, monkeypatch):
    """Пишет экспортированный проект на диск и импортирует main.py — как это сделает пользователь."""
    compiled = compile_project(graph)
    root = tmp_path / "export"
    for path, content in codegen.generate_files(compiled).items():
        (root / path).parent.mkdir(parents=True, exist_ok=True)
        (root / path).write_text(content, "utf-8")
    monkeypatch.setenv("DATABASE_URL", f"sqlite:///{tmp_path / 'exported.sqlite'}")
    for e in compiled.env:  # секреты в экспорт не попадают — задаём их через окружение
        if e.secret:
            monkeypatch.setenv(e.key, e.value)
    for name in [m for m in sys.modules if m == "blocks" or m.startswith("blocks.")]:
        monkeypatch.delitem(sys.modules, name)
    monkeypatch.syspath_prepend(str(root))
    spec = importlib.util.spec_from_file_location("exported_main", root / "main.py")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod.app


@pytest.fixture(params=["interpreted", "exported"])
def client(request, tmp_path, monkeypatch):
    if request.param == "interpreted":
        compiled = compile_project(EXAMPLE)
        db = Database(f"sqlite:///{tmp_path / 'preview.sqlite'}", [m.as_dict() for m in compiled.models])
        return TestClient(build_app(compiled, db))
    return TestClient(exported_app(EXAMPLE, tmp_path, monkeypatch))


def test_hello_uses_env(client):
    assert client.get("/hello").json() == {"message": "Hello, world! Это DragAPI Shop"}
    assert client.get("/hello", params={"name": "Kolya"}).json()["message"].startswith("Hello, Kolya!")


def test_order_crud(client):
    r = client.post("/orders", json={"item": "pen", "price": 400.5, "qty": 3})
    assert r.status_code == 201
    order = r.json()["order"]
    assert order == {"id": 1, "item": "pen", "price": 400.5, "qty": 3, "total": 1201.5, "status": "new",
                     "customer_id": None}
    client.post("/orders", json={"item": "cup", "price": 2})

    listing = client.get("/orders").json()
    assert listing["total"] == 2 and [o["item"] for o in listing["items"]] == ["cup", "pen"]
    assert len(client.get("/orders", params={"per_page": 1}).json()["items"]) == 1

    assert client.get("/orders/1").json() == {"order": {**order, "customer": None}}
    r = client.get("/orders/99")
    assert r.status_code == 404 and r.json() == {"error": "Заказ 99 не найден"}


def test_delete_requires_api_key(client):
    client.post("/orders", json={"item": "pen", "price": 1})
    assert client.delete("/orders/1").status_code == 401
    assert client.delete("/orders/1", headers={"X-API-Key": "wrong"}).status_code == 401
    assert client.delete("/orders/1", headers=KEY).json() == {"deleted": 1}
    assert client.delete("/orders/1", headers=KEY).status_code == 404
    assert client.get("/orders/1").status_code == 404


def test_order_validation(client):
    r = client.post("/orders", json={"item": "p", "price": 1, "qty": 0})
    assert r.status_code == 422
    assert r.json()["errors"] == ["Количество должно быть не меньше 1", "длина item должна быть ≥ 2"]
    assert client.get("/orders").json()["total"] == 0  # невалидный заказ не сохранён


def test_order_type_error(client):
    assert client.post("/orders", json={"item": "pen", "price": "abc"}).status_code == 422
    assert client.get("/orders/x").status_code == 422


def test_python_code_block(client):
    r = client.post("/text/stats", json={"text": "Кот и кот, и ещё один КОТ. Пёс!", "top": 2})
    assert r.status_code == 200
    assert r.json() == {"words": 6, "top": [{"word": "кот", "count": 3}, {"word": "ещё", "count": 1}]}
    assert client.post("/text/stats", json={"text": "a", "top": 50}).status_code == 422


def test_upload(client):
    r = client.post("/upload", files={"file": ("a.txt", b"hello")}, data={"comment": "hi"})
    assert r.json() == {"filename": "a.txt", "size": 5, "comment": "hi"}


def test_openapi(client):
    spec = client.get("/openapi.json").json()
    assert "requestBody" in spec["paths"]["/orders"]["post"]
    get_one = spec["paths"]["/orders/{order_id}"]["get"]
    assert {"200", "404"} <= set(get_one["responses"])
    delete = spec["paths"]["/orders/{order_id}"]["delete"]
    assert delete["security"] and "401" in delete["responses"]
    assert spec["components"]["securitySchemes"]


def test_export_has_no_secrets():
    compiled = compile_project(EXAMPLE)
    source = codegen.generate(compiled)
    assert "dev-secret" not in source
    assert "dev-secret" not in codegen.env_example(compiled)
    files = codegen.generate_files(compiled)
    assert "blocks/code_stats_code.py" in files and "Counter" in files["blocks/code_stats_code.py"]
    assert "from blocks.code_stats_code import run as code_stats_code" in source
    assert "sqlalchemy" in codegen.requirements(compiled)
    compile(source, "main.py", "exec")


# --- проверки графа ---

def _graph_with(mutate):
    g = copy.deepcopy(EXAMPLE)
    mutate(g)
    return g


def _node(g, nid):
    return next(n for n in g["nodes"] if n["id"] == nid)


def _issues(g):
    with pytest.raises(GraphError) as e:
        compile_project(g)
    return " | ".join(i.message for i in e.value.issues)


def test_incompatible_connection():
    g = _graph_with(lambda g: g["edges"].append({"id": "x", "source": "hello", "target": "create-check"}))
    assert "несовместимые" in _issues(g)


def test_unknown_field_in_expr():
    def m(g):
        _node(g, "hello-logic")["data"]["assignments"][0]["expr"] = "nme + '!'"
    assert "неизвестные поля nme" in _issues(_graph_with(m))


def test_json_body_on_get():
    assert "не принимает JSON" in _issues(_graph_with(lambda g: _node(g, "hello-in")["data"].update(source="json")))


def test_missing_response():
    g = _graph_with(lambda g: g.update(edges=[e for e in g["edges"] if e["target"] != "hello-out"]))
    assert "обрывается" in _issues(g)


def test_condition_needs_both_branches():
    g = _graph_with(lambda g: g.update(edges=[e for e in g["edges"] if e["target"] != "get-200"]))
    assert "оба выхода" in _issues(g)


def test_branch_fields_are_scoped():
    # поле, вычисленное в ветке «да», недоступно в ветке «нет»
    def m(g):
        g["nodes"].append({"id": "lg", "type": "logic", "position": {"x": 0, "y": 0},
                           "data": {"assignments": [{"target": "msg", "expr": "'x'"}]}})
        g["edges"] = [e for e in g["edges"] if e["target"] != "get-404"]
        g["edges"] += [{"id": "a", "source": "get-if", "sourceHandle": "true", "target": "lg"},
                       {"id": "b", "source": "lg", "target": "get-404"}]
        _node(g, "get-200")["data"]["body"] = [{"key": "m", "expr": "msg"}]
    assert "неизвестные поля msg" in _issues(_graph_with(m))


def test_db_unknown_model_and_field():
    assert "не найдена" in _issues(_graph_with(lambda g: _node(g, "create-db")["data"].update(model="nope")))

    def m(g):
        _node(g, "create-db")["data"]["values"].append({"field": "color", "expr": "'red'"})
    assert "нет поля «color»" in _issues(_graph_with(m))


def test_auth_requires_declared_env():
    g = _graph_with(lambda g: g.update(env=[e for e in g["env"] if e["key"] != "API_KEY"]))
    assert "API_KEY" in _issues(g)


def test_model_validation():
    def m(g):
        g["models"].append({"name": "bad", "fields": [{"name": "id", "type": "int"}]})
    assert "id создаётся автоматически" in _issues(_graph_with(m))


# --- безопасность выражений ---

@pytest.mark.parametrize("src", [
    "__import__('os')", "().__class__", "open('/etc/passwd')", "x.y", "lambda: 1", "[a for a in b]",
    "env.__class__",
])
def test_expr_rejects(src):
    with pytest.raises(expr.ExprError):
        expr.parse(src)


@pytest.mark.parametrize("src", ["2 ** 1000", "'a' * 10**6", "f'{x:99999}'"])
def test_expr_limits(src):
    with pytest.raises(expr.ExprError):
        expr.evaluate(expr.parse(src), {"x": 1})


def test_expr_env():
    assert expr.evaluate(expr.parse("env['A'] + x"), {"x": "!"}, {"A": "hi"}) == "hi!"
    assert expr.to_python(expr.parse("env['A'] + x")) == "ENV['A'] + ctx['x']"


def test_expr_to_python():
    assert expr.to_python(expr.parse("upper(a) + 'x'")) == "upper(ctx['a']) + 'x'"


# --- миграция схемы ---

def test_schema_adds_new_columns(tmp_path):
    url = f"sqlite:///{tmp_path / 'm.sqlite'}"
    Database(url, [{"name": "t", "fields": [{"name": "a", "type": "str"}]}]).insert("t", {"a": "x"})
    db = Database(url, [{"name": "t", "fields": [{"name": "a", "type": "str"}, {"name": "b", "type": "int"}]}])
    assert db.get("t", 1) == {"id": 1, "a": "x", "b": None}


# --- API платформы целиком ---

def test_platform_run_flow(tmp_path, monkeypatch):
    monkeypatch.setattr(storage, "DATA_DIR", tmp_path / "projects")
    monkeypatch.setattr(storage, "DB_DIR", tmp_path / "db")
    c = TestClient(platform_app, headers={"X-DragAPI": "1"})
    pid = c.post("/api/projects", json={"name": "Demo"}).json()["id"]
    body = {k: EXAMPLE[k] for k in ("name", "nodes", "edges", "models", "env")}
    assert c.put(f"/api/projects/{pid}", json=body).status_code == 200
    assert c.get(f"/run/{pid}/hello").status_code == 404  # ещё не запущен
    assert c.get(f"/api/projects/{pid}/data").status_code == 409
    run = c.post(f"/api/projects/{pid}/run").json()
    assert run["ok"] and len(run["routes"]) == 8
    assert c.post(f"/run/{pid}/orders", json={"item": "pen", "price": 3}).status_code == 201
    assert {"model": "orders", "count": 1} in c.get(f"/api/projects/{pid}/data").json()
    rows = c.get(f"/api/projects/{pid}/data/orders").json()
    assert rows["rows"][0]["item"] == "pen" and "status" in rows["columns"]

    # перезапуск не теряет данные (БД превью персистентна)
    c.post(f"/api/projects/{pid}/run")
    assert c.get(f"/run/{pid}/orders/1").status_code == 200

    assert f"/run/{pid}/openapi.json" in c.get(f"/run/{pid}/docs").text
    logs = c.get(f"/api/projects/{pid}/logs").json()
    assert logs[0]["status"] == 200 and [s["kind"] for s in logs[0]["trace"]][-2:] == ["condition", "response"]
    assert c.get(f"/api/projects/{pid}/export?format=py").text.startswith('"""Сгенерировано')
    assert c.delete(f"/api/projects/{pid}/data/orders").json() == {"ok": True}
    assert c.delete(f"/api/projects/{pid}").json() == {"ok": True}
    assert list((tmp_path / "projects").iterdir()) == []
    assert list((tmp_path / "db").iterdir()) == []
    assert c.get(f"/run/{pid}/hello").status_code == 404


def test_exported_fstrings_are_portable():
    # без f-строк в экспорте нет конструкций, которые есть только в Python 3.12+
    assert expr.to_python(expr.parse("f'#{n:03d} {s!r}'")) == "'#' + format(ctx['n'], '03d') + ' ' + format(repr(ctx['s']), '')"
    source = codegen.generate(compile_project(EXAMPLE))
    generated = source.split("# --- конец runtime ---")[-1].replace("(f'Bearer", "")
    assert not re.search(r"(?<![A-Za-z0-9_])f'", generated)


# --- блок «Python-код» ---

def _code_graph(code, outputs=("y",)):
    g = copy.deepcopy(EXAMPLE)
    _node(g, "stats-code")["data"].update(code=code, outputs=list(outputs))
    _node(g, "stats-out")["data"]["body"] = [{"key": o, "expr": o} for o in outputs]
    return g


@pytest.mark.parametrize("code, message", [
    ("def run(data, env)\n  pass", "синтаксическая ошибка в строке 1"),
    ("x = 1", "функция run(data, env)"),
    ("def run(data):\n    return {}", "ровно run(data, env)"),
    ("async def run(data, env):\n    return {}", "не async"),
])
def test_code_block_compile_errors(code, message):
    assert message in _issues(_code_graph(code))


def _interpreted(graph, tmp_path):
    compiled = compile_project(graph)
    db = Database(f"sqlite:///{tmp_path / 'p.sqlite'}", [m.as_dict() for m in compiled.models])
    return TestClient(build_app(compiled, db))


@pytest.mark.parametrize("code, error", [
    ("def run(data, env):\n    return 1 / 0", "ZeroDivisionError: division by zero (строка 2)"),
    ("def run(data, env):\n    return {'y': 1, 'z': 2}", "необъявленные поля: z"),
    ("def run(data, env):\n    return [1]", "должна вернуть dict"),
    ("def run(data, env):\n    while True:\n        pass", "превышено время выполнения"),
])
def test_code_block_runtime_errors(tmp_path, code, error):
    g = _code_graph(code)
    _node(g, "stats-code")["data"]["timeout"] = 0.5
    r = _interpreted(g, tmp_path).post("/text/stats", json={"text": "a b c"})
    assert r.status_code == 500 and error in r.json()["error"] and r.json()["block"] == "stats-code"


def test_code_block_gets_env_and_trace_has_stdout(tmp_path):
    g = _code_graph("def run(data, env):\n    print('debug', data['top'])\n    return {'y': env['SHOP_NAME']}")
    client = _interpreted(g, tmp_path)
    assert client.post("/text/stats", json={"text": "x"}).json() == {"y": "DragAPI Shop"}
    trace = client.app.state.log[0]["trace"]
    step = next(s for s in trace if s["kind"] == "code")
    assert step["stdout"] == "debug 3\n" and step["sandbox"] == sandbox.detect_backend()


@pytest.mark.skipif(sandbox.detect_backend() != "firejail", reason="нужен firejail")
def test_sandbox_isolation():
    import asyncio

    code = """
import os, socket
def run(data, env):
    try:
        socket.create_connection(("1.1.1.1", 80), timeout=1)
        net = "open"
    except OSError:
        net = "blocked"
    return {"net": net, "project_visible": os.path.exists(%r), "env": sorted(os.environ)}
""" % str(Path(__file__).resolve().parent.parent)
    r = asyncio.run(sandbox.run_code(code, {}, {}))
    assert r.ok, r.error
    assert r.result["net"] == "blocked" and r.result["project_visible"] is False
    assert "PATH" not in r.result["env"]  # окружение сервера не протекает


def test_sandbox_api(tmp_path, monkeypatch):
    monkeypatch.setattr(storage, "DATA_DIR", tmp_path / "projects")
    c = TestClient(platform_app, headers={"X-DragAPI": "1"})
    assert c.get("/api/sandbox").json()["backend"] in ("firejail", "process")
    pid = c.post("/api/projects", json={"name": "sbx"}).json()["id"]
    r = c.post(f"/api/projects/{pid}/sandbox/run", json={
        "code": "def run(data, env):\n    print('hi')\n    return {'s': data['a'] + 1}", "data": {"a": 1}}).json()
    assert r["ok"] and r["result"] == {"s": 2} and r["stdout"] == "hi\n"
    d = c.post("/api/sandbox/describe", json={"firewall": {"egress": {"mode": "filtered"}}}).json()
    assert d["egress"] == "filtered" and ("firewall" in d["details"] or d["backend"] == "process")
    assert c.post("/api/sandbox/describe", json={"sandbox": {"max_file_mb": 999}}).status_code == 400
    check = c.post("/api/firewall/check", json={"target": "10.0.0.5:80", "firewall": {"egress": {"mode": "filtered", "default": "allow"}}}).json()
    assert not check["allowed"] and "локальная сеть" in check["reason"]
    check = c.post("/api/firewall/check", json={"direction": "in", "target": "192.168.1.5", "firewall": {"ingress": {
        "enabled": True, "default": "deny", "rules": [{"action": "allow", "target": "192.168.1.0/24"}]}}}).json()
    assert check["allowed"]
    bad = c.post(f"/api/projects/{pid}/libraries/install", json={"packages": ["--index-url http://evil"]})
    assert bad.status_code == 400
    st = c.post(f"/api/projects/{pid}/libraries/status", json={"packages": []}).json()
    assert st["status"] == "none"


# --- настройки песочницы и библиотеки ---

@pytest.mark.parametrize("line, ok", [
    ("numpy", True), ("requests==2.32.3", True), ("pandas>=2.0,<3", True), ("uvicorn[standard]>=0.29", True),
    ("-e .", False), ("--index-url http://x", False), ("git+https://github.com/x/y", False),
    ("../local.whl", False), ("numpy; os.system('x')", False), ("https://x/y.whl", False),
])
def test_validate_packages(line, ok):
    packages, errors = validate_packages([line])
    assert (packages == [line] and not errors) if ok else (not packages and errors)


def test_sandbox_settings_parse():
    s, errors = SandboxSettings.parse({"backend": "firejail", "network": True, "max_file_mb": 5})
    assert not errors and s.network and s.max_file_mb == 5
    _, errors = SandboxSettings.parse({"backend": "docker", "max_open_files": 3})
    assert len(errors) == 2
    assert "песочница" in _issues(_graph_with(lambda g: g.update(sandbox={"backend": "nope"})))
    assert "библиотеки" in _issues(_graph_with(lambda g: g.update(libraries={"packages": ["-r x.txt"]})))


def test_export_requirements_include_libraries():
    g = _graph_with(lambda g: g.update(libraries={"packages": ["humanize==4.11.0"]}))
    assert "humanize==4.11.0" in codegen.requirements(compile_project(g))


needs_firejail = pytest.mark.skipif(not sandbox.firejail_available(), reason="нужен firejail")


@needs_firejail
def test_sandbox_network_toggle():
    import asyncio
    import socket
    import threading

    server = socket.socket()
    server.bind(("127.0.0.1", 0))
    server.listen()
    port = server.getsockname()[1]
    threading.Thread(target=lambda: server.accept(), daemon=True).start()
    code = f"""
import socket
def run(data, env):
    try:
        socket.create_connection(("127.0.0.1", {port}), timeout=1).close()
        return {{"net": "open"}}
    except OSError as e:
        return {{"net": type(e).__name__}}
"""
    closed = asyncio.run(sandbox.run_code(code, {}, {}, settings=SandboxSettings(network=False)))
    opened = asyncio.run(sandbox.run_code(code, {}, {}, settings=SandboxSettings(network=True)))
    server.close()
    assert closed.result["net"] != "open" and opened.result["net"] == "open"
    r = asyncio.run(sandbox.run_code(code.replace('return {"net": type(e).__name__}', "raise"), {}, {}))
    assert not r.ok and "Firewall" in r.error, r.error


@needs_firejail
def test_sandbox_threads_toggle():
    import asyncio

    code = "import threading\ndef run(data, env):\n    t = threading.Thread(target=lambda: None)\n    t.start(); t.join()\n    return {'ok': 1}"
    blocked = asyncio.run(sandbox.run_code(code, {}, {}, settings=SandboxSettings(threads=False)))
    allowed = asyncio.run(sandbox.run_code(code, {}, {}, settings=SandboxSettings(threads=True)))
    assert not blocked.ok and "разрешите потоки" in blocked.error, blocked.error
    assert allowed.ok, allowed.error


def _pypi_reachable() -> bool:
    import socket
    try:
        socket.create_connection(("pypi.org", 443), timeout=3).close()
        return True
    except OSError:
        return False


@pytest.mark.skipif(not _pypi_reachable(), reason="нет доступа к PyPI")
def test_library_install_and_use(tmp_path):
    import asyncio

    manager = EnvManager(tmp_path / "envs")
    assert manager.status("p1", ["humanize==4.11.0"], True)["status"] == "missing"

    async def scenario():
        job = manager.start_install("p1", ["humanize==4.11.0"], True, sandbox.firejail_available())
        await job.task
        return job

    job = asyncio.run(scenario())
    assert job.state == "ready", job.log
    status = manager.status("p1", ["humanize==4.11.0"], True)
    assert status["status"] == "ready" and "humanize==4.11.0" in status["installed"]
    assert not manager.is_current("p1", ["humanize==4.12.0"], True)

    code = "import humanize\ndef run(data, env):\n    return {'size': humanize.naturalsize(data['n'])}"
    r = asyncio.run(sandbox.run_code(code, {"n": 3_000_000}, {}, venv=manager.venv("p1")))
    assert r.ok, r.error
    assert r.result == {"size": "3.0 MB"}
    # без окружения проекта библиотеки нет
    r = asyncio.run(sandbox.run_code(code, {"n": 1}, {}))
    assert not r.ok and "ModuleNotFoundError" in r.error and "Библиотеки" in r.error
    manager.remove("p1")
    assert manager.venv("p1") is None


# --- firewall ---

def _policy(rules, default="deny", block_private=True, hosts=None):
    hosts = hosts or {}

    async def resolver(host, port):
        if host not in hosts:
            raise OSError("unknown host")
        return hosts[host]

    parsed = [parse_rule(r, allow_hosts=True, allow_ports=True)[0] for r in rules]
    return EgressPolicy("filtered", default, block_private, parsed, resolver)


@pytest.mark.parametrize("rules, default, host, port, allowed, reason", [
    ([{"action": "allow", "target": "api.github.com", "ports": "443"}], "deny", "api.github.com", 443, True, "правилом 1"),
    ([{"action": "allow", "target": "api.github.com", "ports": "443"}], "deny", "api.github.com", 80, False, "нет разрешающего"),
    ([{"action": "allow", "target": "*.github.com"}], "deny", "raw.github.com", 443, True, "правилом 1"),
    ([{"action": "allow", "target": "*.github.com"}], "deny", "github.com", 443, False, "нет разрешающего"),
    ([{"action": "deny", "target": "203.0.113.0/24"}], "allow", "bad.example", 443, False, "запрещено правилом 1"),
    ([], "allow", "good.example", 443, True, "по умолчанию"),
    ([], "allow", "metadata", 80, False, "локальная сеть"),             # 169.254.169.254
    ([{"action": "allow", "target": "*"}], "deny", "intranet", 80, False, "слишком общее"),
    ([{"action": "allow", "target": "intranet"}], "deny", "intranet", 80, True, "правилом 1"),
    ([{"action": "allow", "target": "10.0.0.0/8", "ports": "8000-8100"}], "deny", "10.1.2.3", 8080, True, "правилом 1"),
    ([{"action": "deny", "target": "*.ads.example"}, {"action": "allow", "target": "*"}], "deny", "x.ads.example", 443, False, "запрещено правилом 1"),
])
def test_egress_decisions(rules, default, host, port, allowed, reason):
    import asyncio

    hosts = {"api.github.com": ["140.82.112.5"], "raw.github.com": ["185.199.108.133"],
             "github.com": ["140.82.112.3"], "bad.example": ["203.0.113.7"], "good.example": ["93.184.215.14"],
             "metadata": ["169.254.169.254"], "intranet": ["10.0.0.5"], "x.ads.example": ["93.184.215.20"]}
    verdict = asyncio.run(_policy(rules, default, hosts=hosts).decide(host, port))
    assert verdict.allowed is allowed and reason in verdict.reason, verdict


def test_firewall_parse_errors():
    _, errors = FirewallSettings.parse({"egress": {"mode": "filtered", "rules": [
        {"action": "allow", "target": "http://x.com"}, {"action": "allow", "target": "x.com", "ports": "99999"}]},
        "ingress": {"enabled": True, "default": "deny", "rules": [{"action": "allow", "target": "office.example"}]}})
    text = " | ".join(errors)
    assert "правило 1" in text and "99999" in text and "укажите IP-адрес" in text and "API будет недоступно" in text
    assert "firewall" in _issues(_graph_with(lambda g: g.update(firewall={"egress": {"mode": "wifi"}})))
    fw, _ = FirewallSettings.parse(None, legacy_network=True)
    assert fw.egress.mode == "open"  # старые проекты с «Разрешить сеть»


@needs_firejail
def test_egress_filter_in_sandbox():
    import asyncio
    import threading
    from http.server import BaseHTTPRequestHandler, HTTPServer

    class Handler(BaseHTTPRequestHandler):
        def do_GET(self):
            body = f"hello {self.path}".encode()
            self.send_response(200)
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def log_message(self, *args):
            pass

    server = HTTPServer(("127.0.0.1", 0), Handler)
    port = server.server_address[1]
    threading.Thread(target=server.serve_forever, daemon=True).start()

    policy = _policy([{"action": "allow", "target": "api.test", "ports": str(port)}], "deny",
                     hosts={"api.test": ["127.0.0.1"], "evil.test": ["127.0.0.1"]})
    fw = FirewallSettings(egress=policy)
    code = f"""
import socket, urllib.request

def attempt(fn):
    try:
        return fn()
    except Exception as e:
        return "ERR " + type(e).__name__ + ": " + str(e)

def raw(host):
    s = socket.create_connection((host, {port}), timeout=3)
    s.sendall(b"GET /raw HTTP/1.0\\r\\nHost: x\\r\\n\\r\\n")
    data = b""
    while chunk := s.recv(1000):
        data += chunk
    return data.decode().split("\\r\\n\\r\\n", 1)[-1]

def bypass():
    s = socket.socket()
    s.settimeout(2)
    s.connect(("127.0.0.1", {port}))   # прямое соединение мимо прокси
    return "CONNECTED"

def run(data, env):
    return {{
        "urllib": attempt(lambda: urllib.request.urlopen("http://api.test:{port}/u", timeout=5).read().decode()),
        "raw": attempt(lambda: raw("api.test")),
        "denied": attempt(lambda: urllib.request.urlopen("http://evil.test:{port}/x", timeout=5).read().decode()),
        "denied_raw": attempt(lambda: raw("evil.test")),
        "bypass": attempt(bypass),
    }}
"""
    r = asyncio.run(sandbox.run_code(code, {}, {}, settings=SandboxSettings(), firewall=fw))
    server.shutdown()
    assert r.ok, r.error
    res = r.result
    assert res["urllib"] == "hello /u" and res["raw"] == "hello /raw"
    assert res["denied"].startswith("ERR HTTPError") and "403" in res["denied"]
    assert res["denied_raw"].startswith("ERR ConnectionRefusedError: firewall: evil.test")
    assert res["bypass"].startswith("ERR"), "прямое соединение мимо firewall не должно проходить"
    events = {(e["host"], e["allowed"]) for e in r.network}
    assert ("api.test", True) in events and ("evil.test", False) in events


def _as_client(app, ip):
    """TestClient этой версии Starlette не умеет задавать IP клиента — подставляем его в scope."""
    async def wrapper(scope, receive, send):
        return await app({**scope, "client": (ip, 50000)}, receive, send)
    return wrapper


def _ingress_graph(rules, default="deny", trust_proxy=False):
    return _graph_with(lambda g: g.update(firewall={"ingress": {
        "enabled": True, "default": default, "trust_proxy": trust_proxy, "rules": rules}}))


@pytest.mark.parametrize("mode", ["interpreted", "exported"])
def test_ingress_firewall(tmp_path, monkeypatch, mode):
    from app.runtime import IngressFirewall

    g = _ingress_graph([{"action": "deny", "target": "10.0.0.66"}, {"action": "allow", "target": "10.0.0.0/8"}])
    if mode == "interpreted":
        compiled = compile_project(g)
        db = Database(f"sqlite:///{tmp_path / 'p.sqlite'}", [m.as_dict() for m in compiled.models])
        fw = compiled.firewall.ingress
        app = IngressFirewall(build_app(compiled, db), fw.runtime_rules(), fw.default, fw.trust_proxy)
    else:
        app = exported_app(g, tmp_path, monkeypatch)
        assert "IngressFirewall" in codegen.generate(compile_project(g))

    def status(ip):
        return TestClient(_as_client(app, ip)).get("/hello").status_code

    assert status("10.1.2.3") == 200
    assert status("10.0.0.66") == 403
    assert status("8.8.8.8") == 403
    body = TestClient(_as_client(app, "8.8.8.8")).get("/hello").json()
    assert "запрещено по умолчанию" in body["detail"]


def test_ingress_trust_proxy():
    from app.runtime import IngressFirewall

    async def ok(scope, receive, send):
        await send({"type": "http.response.start", "status": 200, "headers": []})
        await send({"type": "http.response.body", "body": b"ok"})

    rules = [{"action": "allow", "target": "203.0.113.5"}]
    trusted = TestClient(_as_client(IngressFirewall(ok, rules, "deny", trust_proxy=True), "127.0.0.1"))
    assert trusted.get("/", headers={"X-Forwarded-For": "203.0.113.5, 10.0.0.1"}).status_code == 200
    untrusted = TestClient(_as_client(IngressFirewall(ok, rules, "deny", trust_proxy=False), "127.0.0.1"))
    assert untrusted.get("/", headers={"X-Forwarded-For": "203.0.113.5"}).status_code == 403


# --- шаг 1: тестовые запросы, тест-кейсы, экспорт тестов ---

def _platform(tmp_path, monkeypatch):
    monkeypatch.setattr(storage, "DATA_DIR", tmp_path / "projects")
    monkeypatch.setattr(storage, "DB_DIR", tmp_path / "db")
    c = TestClient(platform_app, headers={"X-DragAPI": "1"})
    pid = c.post("/api/projects", json={"name": "T"}).json()["id"]
    return c, pid


def _graph_body(g=None):
    g = g or EXAMPLE
    return {k: g[k] for k in ("name", "nodes", "edges", "models", "env") if k in g} | {"tests": g.get("tests", [])}


def test_test_request_unsaved_graph(tmp_path, monkeypatch):
    c, pid = _platform(tmp_path, monkeypatch)
    graph = _graph_body()
    r = c.post(f"/api/projects/{pid}/test-request", json={
        "graph": graph, "endpoint": "create",
        "request": {"body_type": "json", "body": '{"item": "pen", "price": 2.5, "qty": 2}'}}).json()
    assert r["status"] == 201 and r["body"]["order"]["total"] == 5.0
    assert [s["kind"] for s in r["trace"]] == ["input", "validate", "logic", "db", "response"]
    assert r["trace"][2]["ctx"]["total"] == 5.0

    # секрет через шаблон, ветвление в трассе
    r = c.post(f"/api/projects/{pid}/test-request", json={
        "graph": graph, "endpoint": "delete",
        "request": {"path": {"order_id": "1"}, "headers": {"X-API-Key": "{{env.API_KEY}}"}}}).json()
    assert r["status"] == 200 and r["url"] == "/orders/1"
    assert [s["kind"] for s in r["trace"]][-2:] == ["condition", "response"]

    # отклонено FastAPI до первого блока
    r = c.post(f"/api/projects/{pid}/test-request", json={
        "graph": graph, "endpoint": "create", "request": {"body_type": "json", "body": '{"item": 1}'}}).json()
    assert r["status"] == 422 and r["rejected_before_chain"]

    bad = c.post(f"/api/projects/{pid}/test-request", json={
        "graph": graph, "endpoint": "delete", "request": {"path": {"order_id": "1"}, "headers": {"X": "{{env.NOPE}}"}}})
    assert bad.status_code == 400 and "NOPE" in bad.json()["detail"]["message"]


TESTS = [
    {"id": "a", "name": "создать заказ", "endpoint": "create",
     "request": {"body_type": "json", "body": '{"item": "pen", "price": 3, "qty": 2}'},
     "expect": {"status": 201, "body": '{"order": {"id": 1, "total": 6.0}}'}},
    {"id": "b", "name": "получить заказ", "endpoint": "get", "request": {"path": {"order_id": "1"}},
     "expect": {"status": 200, "body": '{"order": {"item": "pen"}}'}},
    {"id": "c", "name": "удалить без ключа", "endpoint": "delete", "request": {"path": {"order_id": "1"}},
     "expect": {"status": 401}},
    {"id": "d", "name": "удалить с ключом", "endpoint": "delete",
     "request": {"path": {"order_id": "1"}, "headers": {"X-API-Key": "{{env.API_KEY}}"}},
     "expect": {"status": 200, "body": '{"deleted": 1}'}},
]


def test_tests_run(tmp_path, monkeypatch):
    c, pid = _platform(tmp_path, monkeypatch)
    graph = _graph_body() | {"tests": TESTS}
    r = c.post(f"/api/projects/{pid}/tests/run", json={"graph": graph, "reset_data": True}).json()
    assert (r["passed"], r["total"]) == (4, 4), r
    # повторный прогон со сбросом: счётчик id тоже сброшен
    r = c.post(f"/api/projects/{pid}/tests/run", json={"graph": graph, "reset_data": True}).json()
    assert (r["passed"], r["total"]) == (4, 4), r
    # без сброса id уже другой (и удалённые id не переиспользуются) — понятное расхождение
    r = c.post(f"/api/projects/{pid}/tests/run", json={"graph": graph, "tests": TESTS[:1]}).json()
    assert r["passed"] == 0 and r["results"][0]["problems"] == ["$.order.id: ожидалось 1, получено 2"]


def test_match_subset():
    from app.testing import match_subset
    assert match_subset({"a": {"b": 1}}, {"a": {"b": 1, "c": 2}, "x": 0}) == []
    assert match_subset({"a": [1, 2]}, {"a": [1]}) == ["$.a: ожидался список из 2 элементов, получено 1 элементов"]
    assert match_subset({"a": 1}, {}) == ["$.a: нет в ответе"]


def test_exported_tests_run_with_pytest(tmp_path):
    import subprocess

    g = copy.deepcopy(EXAMPLE) | {"tests": TESTS}
    root = tmp_path / "export"
    for path, content in codegen.generate_files(compile_project(g)).items():
        (root / path).parent.mkdir(parents=True, exist_ok=True)
        (root / path).write_text(content, "utf-8")
    env = {"PATH": "/usr/bin:/bin", "API_KEY": "dev-secret", "HOME": str(tmp_path)}
    proc = subprocess.run([sys.executable, "-m", "pytest", "-q", "-p", "no:cacheprovider", "tests"],
                          cwd=root, env=env, capture_output=True, text=True, timeout=120)
    assert proc.returncode == 0, proc.stdout[-2000:] + proc.stderr[-2000:]
    assert "4 passed" in proc.stdout
    assert "dev-secret" not in (root / "tests" / "test_api.py").read_text()


# --- шаг 2: шифрование секретов ---

def test_secrets_encrypted_at_rest(tmp_path, monkeypatch):
    c, pid = _platform(tmp_path, monkeypatch)
    body = _graph_body()
    assert c.put(f"/api/projects/{pid}", json=body).status_code == 200
    raw = (tmp_path / "projects" / f"{pid}.json").read_text()
    assert "dev-secret" not in raw and "value_enc" in raw

    # браузер не получает значение секрета
    env = {e["key"]: e for e in c.get(f"/api/projects/{pid}").json()["env"]}
    assert env["API_KEY"]["value"] == "" and env["API_KEY"]["has_value"] is True
    assert "value_enc" not in env["API_KEY"] and env["SHOP_NAME"]["value"] == "DragAPI Shop"

    # сохраняем из браузера (секрет пустой) — значение сохраняется, в истории тоже только шифр
    masked = c.get(f"/api/projects/{pid}").json()
    body2 = {**body, "env": masked["env"], "name": "T2"}
    c.put(f"/api/projects/{pid}", json=body2)
    for f in (tmp_path / "projects").rglob("*.json"):
        assert "dev-secret" not in f.read_text(), f

    # превью и тестовый запрос используют расшифрованный секрет
    c.post(f"/api/projects/{pid}/run")
    c.post(f"/run/{pid}/orders", json={"item": "pen", "price": 1})
    assert c.delete(f"/run/{pid}/orders/1", headers={"X-API-Key": "dev-secret"}).status_code == 200
    r = c.post(f"/api/projects/{pid}/test-request", json={
        "graph": body2, "endpoint": "hello", "request": {}}).json()
    assert r["status"] == 200
    r = c.post(f"/api/projects/{pid}/test-request", json={
        "graph": body2, "endpoint": "delete",
        "request": {"path": {"order_id": "5"}, "headers": {"X-API-Key": "{{env.API_KEY}}"}}}).json()
    assert r["status"] == 404  # ключ подошёл (иначе 401), заказа 5 нет
    # «Проверить» блока кода тоже получает секрет
    code = "def run(data, env):\n    return {'k': env['API_KEY']}"
    r = c.post(f"/api/projects/{pid}/sandbox/run", json={"code": code, "env": {"API_KEY": ""}}).json()
    assert r["ok"] and r["result"] == {"k": "dev-secret"}

    # смена значения
    body3 = {**body2, "env": [e | ({"value": "new-key"} if e["key"] == "API_KEY" else {}) for e in masked["env"]]}
    c.put(f"/api/projects/{pid}", json=body3)
    c.post(f"/api/projects/{pid}/run")
    assert c.delete(f"/run/{pid}/orders/1", headers={"X-API-Key": "dev-secret"}).status_code == 401


def test_secret_migration_and_wrong_key(tmp_path, monkeypatch):
    from cryptography.fernet import Fernet

    monkeypatch.setattr(storage, "DATA_DIR", tmp_path)
    plain = {"name": "old", "nodes": [], "edges": [],
             "env": [{"key": "TOKEN", "value": "s3cr3t", "secret": True}, {"key": "A", "value": "1"}]}
    (tmp_path / "old-1.json").write_text(json.dumps(plain))
    (tmp_path / "old-1.versions").mkdir()
    (tmp_path / "old-1.versions" / "1.json").write_text(json.dumps(plain))
    assert storage.migrate_secrets() == 2
    assert all("s3cr3t" not in f.read_text() for f in tmp_path.rglob("*.json"))
    assert storage.migrate_secrets() == 0
    env = vault.unseal_env(storage.load("old-1")["env"])
    assert {e["key"]: e["value"] for e in env} == {"TOKEN": "s3cr3t", "A": "1"}

    monkeypatch.setenv("DRAGAPI_SECRET_KEY", Fernet.generate_key().decode())
    vault.reset_cache()
    with pytest.raises(vault.VaultError, match="другим ключом"):
        vault.unseal_env(storage.load("old-1")["env"])


def test_seed_examples(tmp_path, monkeypatch):
    monkeypatch.setattr(storage, "DATA_DIR", tmp_path / "p")
    assert storage.seed_examples() == 1
    assert storage.seed_examples() == 0  # уже есть проекты — не трогаем
    assert "dev-secret" not in (tmp_path / "p" / "example-shop.json").read_text()



# --- шаг 3: связи, миграции, фильтры, пагинация ---

def test_relations_filters_pagination(client):
    alice = client.post("/customers", json={"name": "Alice", "email": "ALICE@x.io"}).json()["customer"]
    assert alice == {"id": 1, "name": "Alice", "email": "alice@x.io"}
    assert client.post("/customers", json={"name": "B", "email": "nope"}).status_code == 422
    for i, (item, price) in enumerate([("pen", 1), ("pencil", 5), ("cup", 30), ("pen holder", 12)]):
        r = client.post("/orders", json={"item": item, "price": price, "customer_id": 1 if i % 2 == 0 else None})
        assert r.status_code == 201
    # заказ на несуществующего клиента — 409, не 500
    r = client.post("/orders", json={"item": "ghost", "price": 1, "customer_id": 99})
    assert r.status_code == 409 and "связь" in r.json()["error"]

    page = client.get("/orders", params={"q": "pen", "per_page": 2, "page": 1}).json()
    assert page["total"] == 3 and page["pages"] == 2 and [o["item"] for o in page["items"]] == ["pen holder", "pencil"]
    page2 = client.get("/orders", params={"q": "pen", "per_page": 2, "page": 2}).json()
    assert [o["item"] for o in page2["items"]] == ["pen"] and page2["items"][0]["customer"]["name"] == "Alice"
    assert client.get("/orders", params={"min_total": 10}).json()["total"] == 2
    assert client.get("/orders", params={"status": "new", "q": "cup"}).json()["total"] == 1
    assert client.get("/orders/1").json()["order"]["customer"] == alice


def _db(tmp_path, models, name="m.sqlite"):
    return Database(f"sqlite:///{tmp_path / name}", models)


def test_migrations_rename_type_extra(tmp_path):
    v1 = [{"id": "m1", "name": "items", "fields": [
        {"id": "a", "name": "title", "type": "str"}, {"id": "b", "name": "qty", "type": "str"},
        {"id": "c", "name": "note", "type": "str"}]}]
    db = _db(tmp_path, v1)
    db.insert("items", {"title": "x", "qty": "42", "note": "n"})
    db.insert("items", {"title": "y", "qty": "7", "note": "n"})
    db.delete("items", 2)
    db.dispose()

    # переименовали поле и модель, сменили тип, убрали поле
    v2 = [{"id": "m1", "name": "products", "fields": [
        {"id": "a", "name": "name", "type": "str"}, {"id": "b", "name": "qty", "type": "int"}]}]
    plan = Database(f"sqlite:///{tmp_path / 'm.sqlite'}", v2, apply_migrations=False).plan()
    ops = {(o["op"], o.get("field") or o.get("old")) for o in plan}
    assert ("rename_table", "items") in ops and ("rename_column", "name") in ops
    assert ("change_type", "qty") in ops and ("extra_column", "note") in ops

    db = _db(tmp_path, v2)
    # данные пережили переименование и смену типа; убранное из модели поле сохранено до явного удаления
    assert db.get("products", 1) == {"id": 1, "name": "x", "qty": 42}
    with db.engine.connect() as conn:
        assert conn.exec_driver_sql("SELECT note FROM products WHERE id = 1").scalar() == "n"
    assert db.insert("products", {"name": "z", "qty": 1})["id"] == 3  # id 2 не переиспользован
    assert db.plan() == [{"op": "extra_column", "model": "products", "field": "note"}]
    db.migrate(drop_extra=True)
    assert "note" not in db.get("products", 1) and db.plan() == []
    db.dispose()


def test_migrations_legacy_db_without_snapshot(tmp_path):
    import sqlalchemy as sa
    engine = sa.create_engine(f"sqlite:///{tmp_path / 'old.sqlite'}")
    with engine.begin() as conn:
        conn.exec_driver_sql("CREATE TABLE orders (id INTEGER PRIMARY KEY, item VARCHAR)")
        conn.exec_driver_sql("INSERT INTO orders (item) VALUES ('legacy')")
    engine.dispose()
    db = Database(f"sqlite:///{tmp_path / 'old.sqlite'}", [{"name": "orders", "fields": [
        {"name": "item", "type": "str"}, {"id": "n", "name": "price", "type": "float"}]}])
    assert db.applied == [{"op": "add_column", "model": "orders", "field": "price", "type": "float"}]
    assert db.get("orders", 1) == {"id": 1, "item": "legacy", "price": None}


@pytest.mark.parametrize("on_delete, expected", [("restrict", "error"), ("cascade", "gone"), ("set_null", "null")])
def test_relation_on_delete(tmp_path, on_delete, expected):
    from app.runtime import DatabaseError
    db = _db(tmp_path, [
        {"id": "c", "name": "customers", "fields": [{"id": "n", "name": "name", "type": "str"}]},
        {"id": "o", "name": "orders", "fields": [
            {"id": "r", "name": "customer_id", "type": "ref", "ref": "customers", "on_delete": on_delete}]}])
    db.insert("customers", {"name": "A"})
    db.insert("orders", {"customer_id": 1})
    if expected == "error":
        with pytest.raises(DatabaseError, match="связь"):
            db.delete("customers", 1)
    else:
        assert db.delete("customers", 1)
        assert (db.get("orders", 1) is None) if expected == "gone" else (db.get("orders", 1)["customer_id"] is None)
    db.clear_all()
    assert db.count("orders") == 0 and db.insert("customers", {"name": "B"})["id"] == 1


def test_filter_ops(tmp_path):
    db = _db(tmp_path, [{"name": "t", "fields": [{"name": "s", "type": "str"}, {"name": "n", "type": "int"}]}])
    for s_, n in [("apple", 1), ("banana", 5), ("cherry", None), ("100%_off", 9)]:
        db.insert("t", {"s": s_, "n": n})

    def ids(filters):
        return [r["id"] for r in db.list("t", filters)]
    assert ids([("n", "gt", 1)]) == [2, 4]
    assert ids([("n", "lte", 5), ("s", "startswith", "b")]) == [2]
    assert ids([("s", "contains", "%_")]) == [4]          # спецсимволы LIKE экранируются
    assert ids([("s", "in", ["apple", "cherry"])]) == [1, 3]
    assert ids([("n", "is_null", True)]) == [3] and ids([("n", "is_null", False)]) == [1, 2, 4]
    assert ids([("n", "ne", 5)]) == [1, 4]                 # NULL не равен и не «не равен» (как в SQL)
    assert ids([("s", "eq", None), ("n", "gte", "")]) == [1, 2, 3, 4]  # пустые фильтры пропускаются
    assert db.count("t", [("n", "gte", 5)]) == 2
    assert [r["id"] for r in db.list("t", [], limit=2, offset=1)] == [2, 3]


def test_schema_plan_api(tmp_path, monkeypatch):
    c, pid = _platform(tmp_path, monkeypatch)
    c.put(f"/api/projects/{pid}", json=_graph_body())
    c.post(f"/api/projects/{pid}/run")
    models = copy.deepcopy(EXAMPLE["models"])
    models[1]["fields"][0]["name"] = "title"  # item -> title (тот же id)
    plan = c.post(f"/api/projects/{pid}/schema/plan", json={"models": models}).json()["ops"]
    assert plan == [{"op": "rename_column", "model": "orders", "old": "item", "field": "title"}]


def test_migration_fields_get_ids_later(tmp_path):
    no_ids = [{"name": "t", "fields": [{"name": "a", "type": "str"}]}]
    _db(tmp_path, no_ids).insert("t", {"a": "x"})
    with_ids = [{"id": "m", "name": "t", "fields": [{"id": "fa", "name": "a", "type": "str"}]}]
    db = _db(tmp_path, with_ids)
    assert db.applied == [] and db.get("t", 1) == {"id": 1, "a": "x"}
    renamed = [{"id": "m", "name": "t", "fields": [{"id": "fa", "name": "b", "type": "str"}]}]
    db.dispose()
    db = _db(tmp_path, renamed)
    assert db.get("t", 1) == {"id": 1, "b": "x"}


def test_rebuild_table_with_relation(tmp_path):
    from app.runtime import DatabaseError
    v1 = [{"id": "c", "name": "customers", "fields": [{"id": "n", "name": "name", "type": "str"}]},
          {"id": "o", "name": "orders", "fields": [
              {"id": "q", "name": "qty", "type": "int"},
              {"id": "r", "name": "customer_id", "type": "ref", "ref": "customers", "on_delete": "restrict"}]}]
    db = _db(tmp_path, v1)
    db.insert("customers", {"name": "A"})
    db.insert("orders", {"qty": 3, "customer_id": 1})
    db.dispose()
    v2 = copy.deepcopy(v1)
    v2[1]["fields"][0]["type"] = "str"          # смена типа -> перестройка таблицы со связью
    v2[1]["fields"][1]["name"] = "client_id"    # и переименование поля-связи
    db = _db(tmp_path, v2)
    assert {o["op"] for o in db.applied} == {"rename_column", "change_type"}
    assert db.get("orders", 1) == {"id": 1, "qty": "3", "client_id": 1}
    with pytest.raises(DatabaseError):          # связь по-прежнему соблюдается
        db.insert("orders", {"qty": "1", "client_id": 42})
    with pytest.raises(DatabaseError):
        db.delete("customers", 1)


# --- шаг 4а: пользователи и права ---

def _auth_platform(tmp_path, monkeypatch):
    monkeypatch.setenv("DRAGAPI_AUTH", "on")
    monkeypatch.setattr(storage, "DATA_DIR", tmp_path / "projects")
    monkeypatch.setattr(storage, "DB_DIR", tmp_path / "db")
    return lambda: TestClient(platform_app, headers={"X-DragAPI": "1"})


def test_auth_setup_login_roles(tmp_path, monkeypatch):
    new = _auth_platform(tmp_path, monkeypatch)
    storage.save("legacy-1", {"name": "Старый проект", "nodes": [], "edges": []})
    admin = new()
    assert admin.get("/api/projects").status_code == 401
    assert admin.get("/api/auth/me").json() == {"auth": True, "user": None, "setup_required": True}
    assert admin.post("/api/auth/setup", json={"username": "boss", "password": "short"}).status_code == 400
    r = admin.post("/api/auth/setup", json={"username": "boss", "password": "very-secret-1"})
    assert r.status_code == 200 and r.json()["user"]["role"] == "admin"
    assert admin.post("/api/auth/setup", json={"username": "x", "password": "very-secret-1"}).status_code == 409
    assert [p["id"] for p in admin.get("/api/projects").json()] == ["legacy-1"]  # старый проект — админу

    admin.post("/api/users", json={"username": "ann", "password": "ann-password", "role": "user"})
    admin.post("/api/users", json={"username": "bob", "password": "bob-password", "role": "user"})
    ids = {u["username"]: u["id"] for u in admin.get("/api/users").json()}

    ann = new()
    assert ann.post("/api/auth/login", json={"username": "ann", "password": "wrong-pass"}).status_code == 401
    assert ann.post("/api/auth/login", json={"username": "ANN", "password": "ann-password"}).status_code == 200
    pid = ann.post("/api/projects", json={"name": "Проект Ann"}).json()["id"]
    assert ann.get("/api/users").status_code == 403                        # не админ
    assert ann.get(f"/api/projects/legacy-1").status_code == 404            # чужой проект «не существует»

    bob = new()
    bob.post("/api/auth/login", json={"username": "bob", "password": "bob-password"})
    assert bob.get(f"/api/projects/{pid}").status_code == 404
    assert ann.put(f"/api/projects/{pid}/members", json={"members": {ids["bob"]: "viewer"}}).status_code == 200
    assert bob.get(f"/api/projects/{pid}").json()["my_role"] == "viewer"
    assert bob.put(f"/api/projects/{pid}", json={"name": "взлом"}).status_code == 403
    assert bob.post(f"/api/projects/{pid}/run").status_code == 403
    assert bob.post(f"/api/projects/{pid}/schema/plan", json={"models": []}).status_code == 200  # только чтение
    ann.put(f"/api/projects/{pid}/members", json={"members": {ids["bob"]: "editor"}})
    assert bob.put(f"/api/projects/{pid}", json={"name": "правка"}).status_code == 200
    assert bob.put(f"/api/projects/{pid}/members", json={"members": {}}).status_code == 403  # участниками — владелец
    assert bob.delete(f"/api/projects/{pid}").status_code == 403
    assert {p["id"]: p["role"] for p in bob.get("/api/projects").json()} == {pid: "editor"}
    assert admin.get(f"/api/projects/{pid}").json()["my_role"] == "owner"  # админ видит всё

    # смена пароля завершает сессии
    assert admin.put(f"/api/users/{ids['bob']}/password", json={"password": "new-bob-pass"}).status_code == 200
    assert bob.get("/api/projects").status_code == 401
    assert ann.post("/api/auth/logout").status_code == 200 and ann.get("/api/projects").status_code == 401
    assert admin.delete(f"/api/users/{ids['boss']}").status_code == 400  # самого себя


def test_auth_csrf_and_bruteforce(tmp_path, monkeypatch):
    new = _auth_platform(tmp_path, monkeypatch)
    c = new()
    c.post("/api/auth/setup", json={"username": "boss", "password": "very-secret-1"})
    plain = TestClient(platform_app, cookies=dict(c.cookies))
    assert plain.post("/api/projects", json={"name": "x"}).status_code == 403   # нет X-DragAPI
    assert plain.get("/api/projects").status_code == 200                        # чтение — можно
    attacker = new()
    for _ in range(5):
        assert attacker.post("/api/auth/login", json={"username": "boss", "password": "nope-nope"}).status_code == 401
    assert attacker.post("/api/auth/login", json={"username": "boss", "password": "very-secret-1"}).status_code == 429


def test_auth_password_storage(tmp_path):
    from app import auth
    s = auth.AuthStore(tmp_path)
    s.create_user("user1", "password-123")
    raw = (tmp_path / "users.json").read_text()
    assert "password-123" not in raw and "scrypt$" in raw
    assert oct((tmp_path / "users.json").stat().st_mode)[-3:] == "600"
    token, _ = s.login("user1", "password-123", "1.1.1.1")
    assert token not in (tmp_path / "sessions.json").read_text()  # хранится только хэш токена


def test_preview_api_stays_public_with_auth(tmp_path, monkeypatch):
    new = _auth_platform(tmp_path, monkeypatch)
    c = new()
    c.post("/api/auth/setup", json={"username": "boss", "password": "very-secret-1"})
    pid = c.post("/api/projects", json={"name": "p"}).json()["id"]
    c.put(f"/api/projects/{pid}", json=_graph_body())
    c.post(f"/api/projects/{pid}/run")
    assert TestClient(platform_app).get(f"/run/{pid}/hello").status_code == 200


# --- шаг 4б: деплой ---

def test_export_has_docker_files():
    files = codegen.generate_files(compile_project(EXAMPLE))
    docker = files["Dockerfile"]
    assert "USER app" in docker and "VOLUME /data" in docker and "sqlite:////data/app.db" in docker
    assert ".env" in files[".dockerignore"] and "127.0.0.1:8000:8000" in files["docker-compose.yml"]
    assert "dev-secret" not in "".join(files.values())


def test_deploy_api_permissions_and_no_docker(tmp_path, monkeypatch):
    from app import main as main_mod
    new = _auth_platform(tmp_path, monkeypatch)
    admin = new()
    admin.post("/api/auth/setup", json={"username": "boss", "password": "very-secret-1"})
    admin.post("/api/users", json={"username": "viewer1", "password": "viewer-pass", "role": "user"})
    pid = admin.post("/api/projects", json={"name": "p"}).json()["id"]
    uid = next(u["id"] for u in admin.get("/api/users").json() if u["username"] == "viewer1")
    admin.put(f"/api/projects/{pid}/members", json={"members": {uid: "viewer"}})
    viewer = new()
    viewer.post("/api/auth/login", json={"username": "viewer1", "password": "viewer-pass"})
    monkeypatch.setattr(main_mod, "docker_available", lambda: False)
    monkeypatch.setattr(main_mod.deployments, "status", _async_value({"docker": False, "container": None}))
    assert viewer.get(f"/api/projects/{pid}/deploy").status_code == 200
    assert viewer.post(f"/api/projects/{pid}/deploy", json={}).status_code == 403
    r = admin.post(f"/api/projects/{pid}/deploy", json={})
    assert r.status_code == 400 and "Docker недоступен" in r.json()["detail"]["message"]


def _async_value(value):
    async def f(*_args, **_kwargs):
        return value
    return f


@pytest.mark.skipif(os.environ.get("DRAGAPI_DOCKER_TESTS") != "1", reason="долгий тест: DRAGAPI_DOCKER_TESTS=1")
def test_real_docker_deploy(tmp_path, monkeypatch):
    import asyncio
    import urllib.request
    from app import deploy
    monkeypatch.setattr(deploy, "DEPLOY_DIR", tmp_path)
    compiled = compile_project(EXAMPLE)
    manager = deploy.DeployManager()

    async def scenario():
        job = manager.start("pytestdeploy", codegen.generate_files(compiled),
                            {e.key: e.value for e in compiled.env}, None, False)
        await job.task
        return job

    job = asyncio.run(scenario())
    try:
        assert job.state == "ready", job.log[-3000:]
        port = manager.settings("pytestdeploy")["port"]
        with urllib.request.urlopen(f"http://127.0.0.1:{port}/hello?name=CI", timeout=5) as r:
            assert json.loads(r.read())["message"].startswith("Hello, CI!")
    finally:
        asyncio.run(manager.stop("pytestdeploy", remove_data=True))
