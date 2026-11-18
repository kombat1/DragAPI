"""Автоматизация: компиляция сценариев, исполнитель (на FakeDriver — настоящие мышь и клавиатура не трогаются),
превращение записи в блоки."""

import threading
import time

import pytest

from app.automation import compile_automation, recording_to_blocks
from app.graph import GraphError
from dragapi_agent.driver import FakeDriver
from dragapi_agent.keys import KeyError_, parse_combo
from dragapi_agent.runner import Runner, render_template


def node(nid, type_, **data):
    return {"id": nid, "type": type_, "position": {"x": 0, "y": 0}, "data": data}


def edge(s, t, handle=None):
    e = {"id": f"{s}-{t}-{handle}", "source": s, "target": t}
    if handle:
        e["sourceHandle"] = handle
    return e


def chain(*nodes, extra_edges=()):
    edges = [edge(a["id"], b["id"]) for a, b in zip(nodes, nodes[1:])]
    return {"name": "t", "kind": "automation", "nodes": list(nodes), "edges": edges + list(extra_edges)}


def run(graph, name="main", driver=None, **kw):
    project = compile_automation(graph)
    sc = project.scenario(name)
    d = driver or FakeDriver()
    events = []
    result = Runner(sc.plan(), d, emit=events.append, env=project.env_values, **kw).run()
    return result, d, events


# --- клавиши ---

def test_parse_combo():
    assert parse_combo("Ctrl+Shift+T") == ["ctrl", "shift", "t"]
    assert parse_combo("control+Return") == ["ctrl", "enter"]
    assert parse_combo("ctrl++") == ["ctrl", "+"]
    assert parse_combo("F5") == ["f5"]
    for bad in ["", "ctrl+", "a+b", "t+ctrl", "ctrl+ctrl", "hyper+x"]:
        with pytest.raises(KeyError_):
            parse_combo(bad)


# --- компиляция ---

def test_compile_basic_and_errors():
    g = chain(node("s", "trigger", name="main", kind="manual"),
              node("m", "mouse_click", x="10", y="20", button="left", clicks="2"),
              node("k", "key_press", keys="ctrl+s", action="tap"))
    plan = compile_automation(g).scenario("main").plan()
    assert [s["op"] for s in plan["steps"]] == ["mouse_click", "key_press"]
    assert plan["steps"][1]["keys"] == ["ctrl", "s"]

    bad = chain(node("s", "trigger", name="main", kind="hotkey", hotkey="a"),
                node("m", "mouse_click", x="10", y=""),
                node("k", "key_press", keys="ctrl+nope"),
                node("w", "wait", seconds="1 +"))
    with pytest.raises(GraphError) as e:
        compile_automation(bad)
    msgs = {(i.node_id, i.message.split(":")[0]) for i in e.value.issues}
    assert ("s", "горячая клавиша должна включать Ctrl, Alt или Super (или быть F1–F20, Pause) — иначе она будет срабатывать при обычном наборе текста") in msgs
    assert any(n == "m" for n, _ in msgs) and any(n == "k" for n, _ in msgs) and any(n == "w" for n, _ in msgs)


def test_compile_cycle_and_loop_back_edge():
    # обычная связь назад — цикл (ошибка)
    g = chain(node("s", "trigger", name="main"), node("a", "wait", seconds="1"), node("b", "wait", seconds="1"),
              extra_edges=[edge("b", "a")])
    with pytest.raises(GraphError, match="цикл"):
        compile_automation(g)
    # связь из тела обратно в блок цикла — «конец тела»
    g = {"nodes": [node("s", "trigger", name="main"), node("r", "repeat", count="2", var="i"),
                   node("c", "mouse_click"), node("end", "log", message="готово")],
         "edges": [edge("s", "r"), edge("r", "c", "body"), edge("c", "r"), edge("r", "end", "done")]}
    steps = compile_automation(g).scenario("main").plan()["steps"]
    assert steps[0]["op"] == "repeat" and [s["op"] for s in steps[0]["body"]] == ["mouse_click"]
    assert steps[1]["op"] == "log"


def test_duplicate_names_and_hotkeys():
    g = {"nodes": [node("a", "trigger", name="x", kind="hotkey", hotkey="ctrl+alt+1"),
                   node("b", "trigger", name="y", kind="hotkey", hotkey="Ctrl+Alt+1"),
                   node("c", "trigger", name="x")], "edges": []}
    with pytest.raises(GraphError) as e:
        compile_automation(g)
    text = " ".join(i.message for i in e.value.issues)
    assert "уже занято" in text and "уже есть" in text


# --- исполнитель ---

def test_runner_mouse_keyboard_and_vars():
    g = chain(node("s", "trigger", name="main"),
              node("v", "set_vars", assignments=[{"target": "n", "expr": "3"}]),
              node("m", "mouse_move", x="100", y="n * 10", duration="0"),
              node("c", "mouse_click", button="right", clicks="1"),
              node("t", "key_type", text="Привет {n}!", interval_ms="0", enter=True),
              node("k", "key_press", keys="ctrl+a", action="tap", times="2"))
    result, d, events = run(g)
    assert result["status"] == "ok", result
    assert result["vars"] == {"n": 3}
    acts = d.actions
    assert acts[0] == ("move", 100, 30)
    assert ("click", "right", 1, 100, 30) in acts
    typed = "".join(a[1] for a in acts if a[0] == "type")
    assert typed == "Привет 3!"
    keys = [a for a in acts if a[0] in ("key_down", "key_up")]
    assert keys == [("key_down", "enter"), ("key_up", "enter"),
                    ("key_down", "ctrl"), ("key_down", "a"), ("key_up", "a"), ("key_up", "ctrl")] * 1 + \
        [("key_down", "ctrl"), ("key_down", "a"), ("key_up", "a"), ("key_up", "ctrl")]
    assert [e["node"] for e in events if e.get("status") == "start"] == ["v", "m", "c", "t", "k"]


def test_runner_loops_condition_autoclick():
    g = {"nodes": [node("s", "trigger", name="main"),
                   node("r", "repeat", count="3", var="i"),
                   node("if", "condition", expr="i == 2"),
                   node("c", "mouse_click", x="i", y="i"),
                   node("sk", "log", message="пропуск {i}"),
                   node("w", "while", expr="n < 2", var="", max_iterations="10"),
                   node("inc", "set_vars", assignments=[{"target": "n", "expr": "n + 1"}]),
                   node("ac", "autoclick", x="5", y="5", interval_ms="10", mode="count", count="4", target="clicked")],
         "edges": [edge("s", "r"), edge("r", "if", "body"), edge("if", "c", "true"), edge("if", "sk", "false"),
                   edge("r", "w", "done"), edge("w", "inc", "body"), edge("w", "ac", "done")]}
    result, d, events = run(g, inputs={"n": 0})
    assert result["status"] == "ok", result
    clicks = [a for a in d.actions if a[0] == "click"]
    assert clicks[0] == ("click", "left", 1, 2, 2)
    assert len(clicks) == 1 + 4 and result["vars"]["clicked"] == 4 and result["vars"]["n"] == 2
    assert [e["message"] for e in events if e["type"] == "log"][:2] == ["пропуск 1", "пропуск 3"]


def test_runner_errors_limits_stop_and_failsafe():
    g = chain(node("s", "trigger", name="main"), node("w", "while", expr="True", max_iterations="5"))
    g["nodes"].append(node("b", "wait", seconds="0"))
    g["edges"].append(edge("w", "b", "body"))
    result, _, events = run(g)
    assert result["status"] == "error" and "превысил 5" in result["message"]
    assert events[-1]["node"] == "w" and events[-1]["status"] == "error"

    g = chain(node("s", "trigger", name="main"), node("m", "mouse_move", x="'abc'", y="1"))
    result, _, _ = run(g)
    assert result["status"] == "error" and "ожидалось число" in result["message"]

    # лимит действий
    g = chain(node("s", "trigger", name="main"), node("a", "autoclick", interval_ms="10", mode="count", count="50"))
    result, d, _ = run(g, limits={"max_actions": 5})
    assert result["status"] == "error" and "больше 5 действий" in result["message"]

    # остановка извне посреди паузы — и зажатые клавиши отпускаются
    g = chain(node("s", "trigger", name="main"), node("k", "key_press", keys="shift", action="down"),
              node("w", "wait", seconds="30"))
    stop = threading.Event()
    threading.Timer(0.2, stop.set).start()
    began = time.monotonic()
    result, d, _ = run(g, stop_event=stop)
    assert result["status"] == "stopped" and time.monotonic() - began < 2
    assert d.actions[-1] == ("key_up", "shift")

    # аварийный угол: курсор в (0, 0) — стоп
    g = chain(node("s", "trigger", name="main"), node("c", "mouse_click"))
    result, d, _ = run(g, driver=FakeDriver(pos=(0, 0)))
    assert result["status"] == "stopped" and "углу" in result["message"]
    assert not [a for a in d.actions if a[0] == "click"]
    result, _, _ = run(g, driver=FakeDriver(pos=(0, 0)), limits={"failsafe_corner": False})
    assert result["status"] == "ok"


def test_runner_stop_block_and_server_ops():
    g = chain(node("s", "trigger", name="main"),
              node("c", "code", code="def run(data, env):\n    return {'n': data['n'] * 2}\n", outputs=["n"]),
              node("h", "http", method="POST", url="https://api.test/{n}", body=[{"key": "v", "expr": "n"}],
                   target="resp"),
              node("x", "stop", status="error", message="n={n}"))
    project = compile_automation(g)
    sc = project.scenario("main")
    assert sc.code["c"]["outputs"] == ["n"] and sc.http["h"] == {"method": "POST", "timeout": 10.0}
    calls = []

    def server_op(op, nid, payload):
        calls.append((op, nid, payload))
        return {"n": payload["data"]["n"] * 2} if op == "code" else {"status": 200, "json": {"ok": True}}

    result = Runner(sc.plan(), FakeDriver(), inputs={"n": 2}, server_op=server_op).run()
    assert result["status"] == "error" and result["message"] == "n=4"
    assert calls[1] == ("http", "h", {"url": "https://api.test/4", "body": {"v": 4}})
    assert result["vars"]["resp"]["status"] == 200
    # без сервера такие шаги честно падают
    result = Runner(sc.plan(), FakeDriver(), inputs={"n": 2}).run()
    assert result["status"] == "error" and "сервере" in result["message"]


def test_render_template():
    assert render_template("a {x + 1} {{lit}}", {"x": 1}, {}) == "a 2 {lit}"


# --- запись ---

def test_recording_to_blocks():
    ev = [
        {"t": 0.0, "kind": "down", "button": "left", "x": 10, "y": 10},
        {"t": 0.05, "kind": "up", "button": "left", "x": 10, "y": 10},
        {"t": 0.2, "kind": "down", "button": "left", "x": 11, "y": 10},
        {"t": 0.25, "kind": "up", "button": "left", "x": 11, "y": 10},          # двойной щелчок
        {"t": 1.5, "kind": "key_down", "key": "П"}, {"t": 1.6, "kind": "key_down", "key": "р"},
        {"t": 1.7, "kind": "key_down", "key": "space"}, {"t": 1.8, "kind": "key_down", "key": "{"},
        {"t": 2.0, "kind": "key_down", "key": "ctrl"}, {"t": 2.1, "kind": "key_down", "key": "s"},
        {"t": 2.2, "kind": "key_up", "key": "ctrl"},
        {"t": 2.3, "kind": "key_down", "key": "tab"}, {"t": 2.5, "kind": "key_down", "key": "tab"},
        {"t": 3.0, "kind": "down", "button": "left", "x": 100, "y": 100},
        {"t": 3.4, "kind": "up", "button": "left", "x": 300, "y": 200},       # перетаскивание
        {"t": 3.5, "kind": "scroll", "dx": 0, "dy": -1, "x": 5, "y": 5},
        {"t": 3.6, "kind": "scroll", "dx": 0, "dy": -1, "x": 5, "y": 5},
        {"t": 5.0, "kind": "down", "button": "left", "x": 900, "y": 10},     # «Остановить запись»
        {"t": 5.1, "kind": "up", "button": "left", "x": 900, "y": 10},
    ]
    blocks = recording_to_blocks(ev, drop_last_click=True)
    assert [b["type"] for b in blocks] == ["mouse_click", "wait", "key_type", "key_press", "key_press",
                                          "wait", "mouse_drag", "mouse_scroll"]
    assert blocks[0]["data"]["clicks"] == "2"
    assert blocks[1]["data"]["seconds"] == "1.2"
    assert blocks[2]["data"]["text"] == "Пр {{"
    assert blocks[3]["data"]["keys"] == "ctrl+s"
    assert blocks[4]["data"] == {"keys": "tab", "action": "tap", "times": "2"}
    assert blocks[5]["data"]["seconds"] == "0.5"
    assert blocks[6]["data"]["x2"] == "300" and blocks[7]["data"]["dy"] == "-2"
    # и всё это компилируется обратно в сценарий
    nodes = [node("s", "trigger", name="main")] + [node(f"b{i}", b["type"], **b["data"]) for i, b in enumerate(blocks)]
    project = compile_automation(chain(*nodes))
    result = Runner(project.scenario("main").plan(), FakeDriver()).run()
    assert result["status"] == "ok", result


# --- платформа + удалённый агент (FakeDriver) по настоящему WebSocket ---

import json as _json

from fastapi.testclient import TestClient

from app import main as platform_main, storage
from dragapi_agent.agent import Agent


def _connect_agent(client, token, driver):
    """Агент в потоке: читает сообщения платформы из WebSocket и отвечает в него."""
    ctx = client.websocket_connect("/api/agents/ws", headers={"Authorization": f"Bearer {token}"})
    ws = ctx.__enter__()
    lock = threading.Lock()

    def send(msg):
        with lock:
            ws.send_json(msg)

    agent = Agent(send, name="тестовый", driver_factory=lambda: driver, listeners=False, notify=False)
    send(agent.hello())

    def pump():
        try:
            while True:
                agent.handle(ws.receive_json())
        except Exception:  # noqa: BLE001 — соединение закрыто
            pass

    threading.Thread(target=pump, daemon=True).start()
    return agent, ctx


def _wait(fn, timeout=10):
    end = time.monotonic() + timeout
    while time.monotonic() < end:
        value = fn()
        if value:
            return value
        time.sleep(0.05)
    raise AssertionError("не дождались")


def test_platform_runs_scenario_on_remote_agent(tmp_path, monkeypatch):
    monkeypatch.setattr(storage, "DATA_DIR", tmp_path / "projects")
    monkeypatch.setattr(storage, "DB_DIR", tmp_path / "db")
    with TestClient(platform_main.app, headers={"X-DragAPI": "1"}) as client:
        hub = platform_main.hub
        assert client.get("/api/agents").json() == []          # локальный агент в тестах выключен
        created = client.post("/api/agents", json={"name": "Офисный ПК"}).json()
        token, agent_id = created["token"], created["agent"]["id"]

        project = client.post("/api/projects", json={"name": "Кликер", "kind": "automation"}).json()
        pid = project["id"]
        assert project["kind"] == "automation" and project["nodes"][0]["type"] == "trigger"
        assert client.get("/api/projects").json()[0]["kind"] == "automation"

        graph = {"name": "Кликер", "kind": "automation",
                 "automation": {"agent": agent_id, "enabled": False},
                 "env": [{"key": "GREETING", "value": "Привет", "secret": True}],
                 "nodes": [node("start", "trigger", name="main", kind="manual", params=["who"]),
                           node("c", "mouse_click", x="10", y="20"),
                           node("py", "code", code="def run(data, env):\n    return {'msg': env['GREETING'] + ', ' + data['who']}\n",
                                outputs=["msg"]),
                           node("t", "key_type", text="{msg}", interval_ms="0")],
                 "edges": [edge("start", "c"), edge("c", "py"), edge("py", "t")]}
        saved = client.put(f"/api/projects/{pid}", json=graph).json()
        assert saved["env"][0]["value"] == "" and saved["env"][0]["has_value"]   # секрет зашифрован
        v = client.post("/api/validate", json=graph).json()
        assert v["ok"] and v["scenarios"][0]["params"] == ["who"]

        r = client.post(f"/api/projects/{pid}/automation/run", json={"scenario": "main", "inputs": {"who": "мир"}})
        assert r.status_code == 409 and "не в сети" in r.json()["detail"]

        driver = FakeDriver()
        agent, ctx = _connect_agent(client, token, driver)
        try:
            _wait(lambda: client.get("/api/agents").json()[0]["online"])
            assert client.get("/api/agents").json()[0]["info"]["name"] == "тестовый"

            r = client.post(f"/api/projects/{pid}/automation/run", json={"scenario": "main", "inputs": {"who": "мир"}})
            assert r.status_code == 200, r.text
            run_id = r.json()["id"]
            run = _wait(lambda: (lambda d: d if d["status"] != "running" else None)(
                client.get(f"/api/projects/{pid}/automation/runs/{run_id}").json()), timeout=30)
            assert run["status"] == "ok", run
            assert run["vars"]["msg"] == "Привет, мир"
            assert "".join(a[1] for a in driver.actions if a[0] == "type") == "Привет, мир"
            assert ("click", "left", 1, 10, 20) in driver.actions
            assert [e["node"] for e in run["events"] if e.get("status") == "start"] == ["c", "py", "t"]
            assert client.get(f"/api/projects/{pid}/automation/runs").json()[0]["id"] == run_id

            # несохранённый граф из редактора + остановка
            graph2 = {**graph, "nodes": graph["nodes"][:1] + [node("w", "wait", seconds="30")],
                      "edges": [edge("start", "w")]}
            r = client.post(f"/api/projects/{pid}/automation/run",
                            json={"scenario": "main", "inputs": {"who": "x"}, "graph": graph2})
            run_id = r.json()["id"]
            r2 = client.post(f"/api/projects/{pid}/automation/run", json={"scenario": "main", "inputs": {"who": "x"}})
            assert r2.status_code == 409 and "занят" in r2.json()["detail"]
            client.post(f"/api/projects/{pid}/automation/runs/{run_id}/stop")
            run = _wait(lambda: (lambda d: d if d["status"] != "running" else None)(
                client.get(f"/api/projects/{pid}/automation/runs/{run_id}").json()))
            assert run["status"] == "stopped"

            # вебхук: только включённый сценарий с запуском по HTTP и верным токеном
            graph["nodes"][0]["data"]["kind"] = "webhook"
            graph["automation"]["enabled"] = True
            client.put(f"/api/projects/{pid}", json=graph)
            assert client.post(f"/hooks/{pid}/main", json={"who": "hook"}).status_code == 401
            hook_token = client.post(f"/api/projects/{pid}/automation/hook-token").json()["token"]
            assert client.get(f"/api/projects/{pid}").json()["automation"]["has_hook_token"] is True
            client.put(f"/api/projects/{pid}", json=graph)   # пересохранение из редактора токен не теряет
            auth_h = {"Authorization": f"Bearer {hook_token}"}
            r = client.post(f"/hooks/{pid}/main", json={"who": "hook"}, headers=auth_h)
            assert r.status_code == 200 and r.json()["status"] == "ok" and r.json()["vars"]["msg"] == "Привет, hook"
            assert client.post(f"/hooks/{pid}/main", json={}, headers=auth_h).status_code == 409   # нет параметра
            assert client.post(f"/hooks/{pid}/other", json={}, headers=auth_h).status_code == 404

            # запись: агент присылает сырые события — платформа превращает их в блоки
            rec = client.post(f"/api/projects/{pid}/automation/record", json={"countdown": 0}).json()
            _wait(lambda: client.get(f"/api/projects/{pid}/automation/record/{rec['id']}").json()["state"]
                  in ("recording", "error"))
            state = client.get(f"/api/projects/{pid}/automation/record/{rec['id']}").json()
            assert state["state"] == "recording", state
            # щелчок задолго до остановки — настоящий; щелчок прямо перед ней — по кнопке «Закончить» в браузере
            agent.recording["t0"] = time.monotonic() - 10
            agent.recording["events"] += [{"t": 1.0, "kind": "down", "button": "left", "x": 5, "y": 6},
                                          {"t": 1.1, "kind": "up", "button": "left", "x": 5, "y": 6},
                                          {"t": 9.6, "kind": "down", "button": "left", "x": 900, "y": 10},
                                          {"t": 9.7, "kind": "up", "button": "left", "x": 900, "y": 10}]
            client.post(f"/api/projects/{pid}/automation/record/{rec['id']}/stop")
            done = _wait(lambda: (lambda d: d if d["state"] == "done" else None)(
                client.get(f"/api/projects/{pid}/automation/record/{rec['id']}").json()))
            assert [b["type"] for b in done["blocks"]] == ["mouse_click"]
            assert done["blocks"][0]["data"]["x"] == "5"

            # позиция курсора
            p = client.post(f"/api/projects/{pid}/automation/pick-point", json={"countdown": 0}).json()
            point = _wait(lambda: (lambda d: d if "x" in d else None)(
                client.get(f"/api/projects/{pid}/automation/pick-point/{p['id']}").json()))
            assert (point["x"], point["y"]) == driver.position()

            # горячие клавиши уходят агенту, когда сценарий включён
            graph["nodes"][0]["data"].update(kind="hotkey", hotkey="ctrl+alt+9")
            client.put(f"/api/projects/{pid}", json=graph)
            _wait(lambda: frozenset(["ctrl", "alt", "9"]) in agent.hotkeys)
            graph["automation"]["enabled"] = False
            client.put(f"/api/projects/{pid}", json=graph)
            _wait(lambda: not agent.hotkeys)

            # API-проект нельзя запускать как сценарий, а сценарий — экспортировать
            assert client.get(f"/api/projects/{pid}/export").status_code == 400
            client.delete(f"/api/agents/{agent_id}")
            _wait(lambda: not client.get("/api/agents").json())
        finally:
            ctx.__exit__(None, None, None)


def test_agent_permissions(tmp_path, monkeypatch):
    from app import auth
    monkeypatch.setenv("DRAGAPI_AUTH", "on")
    hub = platform_main.hub
    admin = auth.store.create_user("boss", "very-secret-1", "admin")
    ann = auth.store.create_user("ann", "ann-password", "user")
    rec, _ = hub.create_agent("ПК Ann", ann["id"])
    other, _ = hub.create_agent("Чужой", admin["id"])
    assert hub.can_use(admin, "local") and hub.can_use(admin, other["id"])
    assert not hub.can_use(ann, "local")            # рабочий стол сервера — только администраторам
    assert hub.can_use(ann, rec["id"]) and not hub.can_use(ann, other["id"])
    assert not hub.can_use(None, rec["id"])
    hub.update_agent(other["id"], users=[ann["id"], "ghost"])
    assert hub.can_use(ann, other["id"]) and hub.public(hub._read()[1])["users"] == [ann["id"]]
    monkeypatch.setenv("DRAGAPI_AUTH", "off")
    assert not hub.can_use(None, "local")           # пользователи есть — вход обязателен и для агентов


def test_http_block_respects_firewall(tmp_path, monkeypatch):
    import asyncio
    from app.agents import Run
    g = chain(node("s", "trigger", name="main"), node("h", "http", url="https://example.com/"))
    compiled = compile_automation(g)            # firewall по умолчанию: сеть для кода закрыта
    run = Run("r1", "p", "main", "a", "manual", None, {}, compiled=compiled)
    with pytest.raises(RuntimeError, match="сеть для проекта закрыта"):
        asyncio.run(platform_main.hub.server_op(run, "http", "h", {"url": "https://example.com/"}))
    compiled = compile_automation({**g, "firewall": {"egress": {"mode": "open"}}})
    run = Run("r2", "p", "main", "a", "manual", None, {}, compiled=compiled)
    with pytest.raises(RuntimeError, match="http:// или https://"):
        asyncio.run(platform_main.hub.server_op(run, "http", "h", {"url": "file:///etc/passwd"}))


# --- API-проект: блок «Запустить сценарий» ---

def test_api_block_runs_scenario(monkeypatch):
    from collections import deque

    from app import codegen, interpreter
    from app.graph import compile_project
    from app.runtime import ScenarioError

    g = {"name": "api", "env": [{"key": "SCENARIO_TOKEN", "value": "tok", "secret": True}],
         "nodes": [node("ep", "endpoint", method="POST", path="/go", summary=""),
                   node("in", "input", source="json", fields=[{"name": "who", "type": "str", "required": True}]),
                   node("sc", "scenario", project="kliker-abc123", scenario="main",
                        params=[{"key": "who", "expr": "upper(who)"}], target="res", token_env="SCENARIO_TOKEN",
                        url_env="DRAGAPI_URL"),
                   node("resp", "response", status=200, body=[{"key": "msg", "expr": "res['vars']['msg']"}])],
         "edges": [edge("ep", "in"), edge("in", "sc"), edge("sc", "resp")]}
    project = compile_project(g)
    calls = []

    async def fake_call(base, pid, name, token, params, timeout=65):
        calls.append((base, pid, name, token, params))
        if params["who"] == "FAIL":
            raise ScenarioError("сценарий «main» завершился: error — нет окна")
        return {"status": "ok", "vars": {"msg": "hi " + params["who"]}, "run_id": "r1"}

    monkeypatch.setattr(interpreter, "call_scenario", fake_call)
    monkeypatch.setenv("DRAGAPI_PORT", "9999")
    app = interpreter.build_app(project, None, deque(), lambda: None)
    with TestClient(app) as c:
        r = c.post("/go", json={"who": "ann"})
        assert r.status_code == 200 and r.json() == {"msg": "hi ANN"}
        assert calls[0] == ("http://127.0.0.1:9999", "kliker-abc123", "main", "tok", {"who": "ANN"})
        r = c.post("/go", json={"who": "fail"})
        assert r.status_code == 502 and "нет окна" in r.json()["error"] and r.json()["block"] == "sc"

    files = codegen.generate_files(project)
    assert "call_scenario(" in files["main.py"] and "httpx" in files["requirements.txt"]
    compile(files["main.py"], "main.py", "exec")

    # без токена в переменных — предупреждение; без проекта — ошибка
    g2 = {**g, "env": []}
    assert any("SCENARIO_TOKEN" in w.message for w in compile_project(g2).warnings)
    g3 = {**g, "nodes": [*g["nodes"][:2], node("sc", "scenario", project="", scenario="main", target="res",
                                               token_env="SCENARIO_TOKEN"), g["nodes"][3]]}
    with pytest.raises(GraphError, match="проект-автоматизацию"):
        compile_project(g3)
