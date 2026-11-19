"""Автоматизация: «Если шаг не удался» (повторы, пропуск, ветка «ошибка») и подпрограммы
(«Выполнить сценарий»). Исполнитель — на FakeDriver, серверные шаги — подставной server_op."""

import pytest

from app.automation import compile_automation
from app.graph import GraphError
from dragapi_agent.driver import FakeDriver
from dragapi_agent.runner import Runner
from tests.test_automation import chain, edge, node


def flaky(fail_times, message="сервер недоступен"):
    """server_op: HTTP падает первые fail_times раз, потом отвечает."""
    calls = []

    def op(kind, nid, payload):
        calls.append((kind, nid))
        if kind == "http" and sum(1 for k, _ in calls if k == "http") <= fail_times:
            raise RuntimeError(message)
        return {"status": 200}
    op.calls = calls
    return op


def http(nid="h", **on_error):
    data = {"method": "GET", "url": "https://api.test/", "target": "resp"}
    if on_error:
        data["on_error"] = on_error
    return node(nid, "http", **data)


def go(graph, server_op=None, name="main", inputs=None):
    sc = compile_automation(graph).scenario(name)
    events = []
    result = Runner(sc.plan(), FakeDriver(), emit=events.append, server_op=server_op, inputs=inputs).run()
    return result, events


START = node("s", "trigger", name="main")
AFTER = node("a", "set_vars", assignments=[{"target": "after", "expr": "1"}])


def test_retry_until_success():
    op = flaky(2)
    result, events = go(chain(START, http(retries="3", delay="0"), AFTER), op)
    assert result["status"] == "ok" and result["vars"]["after"] == 1
    assert [e["status"] for e in events if e.get("node") == "h" and e["status"] != "start"] == \
        ["retry", "retry", "recovered"]
    assert [e["attempt"] for e in events if e.get("status") == "retry"] == [1, 2]


def test_retries_exhausted_stop_skip():
    result, events = go(chain(START, http(retries="2", delay="0"), AFTER), flaky(10))
    assert result["status"] == "error" and "сервер недоступен" in result["message"]
    assert "after" not in result["vars"] and result["vars"]["error"]["attempts"] == 3
    # без настройки — как раньше: одна попытка и остановка
    result, events = go(chain(START, http(), AFTER), op := flaky(10))
    assert result["status"] == "error" and len(op.calls) == 1
    # «пропустить шаг» — сценарий идёт дальше, ошибка в переменной
    result, events = go(chain(START, http(then="skip", var="err"), AFTER), flaky(10))
    assert result["status"] == "ok" and result["vars"]["after"] == 1
    assert result["vars"]["err"] == {"message": "сервер недоступен", "node": "h", "block": "http", "attempts": 1}
    assert any(e.get("status") == "failed" and e["then"] == "skip" for e in events)


def test_error_branch():
    on_err = node("e", "set_vars", assignments=[{"target": "handled", "expr": "error['message']"}])
    g = chain(START, http(retries="1", then="branch"), AFTER)
    g["nodes"].append(on_err)
    g["edges"].append(edge("h", "e", "error"))
    plan = compile_automation(g).scenario("main").plan()
    assert plan["steps"][0]["next"][0]["node"] == "a" and plan["steps"][0]["on_fail"][0]["node"] == "e"
    result, _ = go(g, flaky(10))
    assert result["status"] == "ok" and result["vars"]["handled"] == "сервер недоступен" and "after" not in result["vars"]
    result, _ = go(g, flaky(1))              # вторая попытка удалась — обычный путь
    assert result["vars"]["after"] == 1 and "handled" not in result["vars"]


def test_error_branch_on_branching_block_and_compile_rules():
    # у «Найти окно» уже есть да/нет — ветка «ошибка» добавляется третьей
    g = chain(START, node("w", "window_find", title="Блокнот", match="contains", mode="appear", wait="0",
                          target="win", on_error={"then": "branch"}))
    g["nodes"] += [node("y", "log", message="да"), node("n", "log", message="нет"), node("e", "log", message="err")]
    g["edges"] += [edge("w", "y", "true"), edge("w", "n", "false"), edge("w", "e", "error")]
    step = compile_automation(g).scenario("main").plan()["steps"][0]
    assert [step[k][0]["node"] for k in ("then", "else", "on_fail")] == ["y", "n", "e"]
    # выход «ошибка» без настройки «перейти по ветке» — ошибка проекта
    g2 = chain(START, http(retries="2"), AFTER, extra_edges=[edge("h", "a", "error")])
    with pytest.raises(GraphError, match="выход «ошибка»"):
        compile_automation(g2)
    for bad, msg in [({"retries": "500"}, "повторить"), ({"delay": "-1"}, "пауза"), ({"then": "x"}, "неизвестное"),
                     ({"then": "skip", "var": "1x"}, "переменная")]:
        with pytest.raises(GraphError, match=msg):
            compile_automation(chain(START, http(**bad)))
    # не подключённая ветка — предупреждение, при ошибке сценарий останавливается
    g3 = chain(START, http(then="branch"), AFTER)
    project = compile_automation(g3)
    assert any("не подключена" in w.message for w in project.warnings)
    result, _ = go(g3, flaky(10))
    assert result["status"] == "error"


def test_limits_and_nested_errors_are_not_retried():
    # ошибка внутри ветки «да» не повторяет сам блок-развилку
    g = chain(START, node("c", "condition", expr="True", on_error={"retries": "5", "then": "skip"}))
    g["nodes"].append(http("inner"))
    g["edges"].append(edge("c", "inner", "true"))
    g["nodes"].append(node("f", "log", message="нет"))
    g["edges"].append(edge("c", "f", "false"))
    op = flaky(10)
    result, events = go(g, op)
    assert result["status"] == "error" and len(op.calls) == 1
    assert not any(e.get("status") == "retry" for e in events)


def test_subroutine_params_result_and_isolation():
    login = [node("s2", "trigger", name="login", params=["user"]),
             node("l1", "set_vars", assignments=[{"target": "token", "expr": "'t-' + user"}]),
             node("l2", "stop", status="ok", message="")]
    main = [START,
            node("v", "set_vars", assignments=[{"target": "mine", "expr": "7"}]),
            node("c", "call_scenario", scenario="login", params=[{"key": "user", "expr": "'ann'"}], target="res"),
            node("after", "set_vars", assignments=[{"target": "tok", "expr": "res['token']"}])]
    g = chain(*main)
    g["nodes"] += login
    g["edges"] += [edge("s2", "l1"), edge("l1", "l2")]
    plan = compile_automation(g).scenario("main").plan()
    assert set(plan["subs"]) == {"login"}
    result, events = go(g)
    assert result["status"] == "ok" and result["vars"]["tok"] == "t-ann"
    assert result["vars"]["res"] == {"user": "ann", "token": "t-ann"}   # подпрограмма не видит mine
    assert result["vars"]["mine"] == 7
    assert any(e.get("node") == "l1" and e["status"] == "start" for e in events)


def test_subroutine_failure_uses_callers_policy_and_server_ops():
    sub = [node("s2", "trigger", name="fetch"), http("sub_h")]
    g = chain(START, node("c", "call_scenario", scenario="fetch", params=[], target="r",
                          on_error={"retries": "1", "then": "skip"}), AFTER)
    g["nodes"] += sub
    g["edges"] += [edge("s2", "sub_h")]
    project = compile_automation(g)
    assert "sub_h" in project.scenario("main").http      # HTTP-блок подпрограммы выполнит сервер
    result, events = go(g, flaky(10))
    assert result["status"] == "ok" and result["vars"]["after"] == 1
    assert "сценарий «fetch»" in result["vars"]["error"]["message"]
    assert any(e.get("node") == "sub_h" and e.get("status") == "error" for e in events)
    result, _ = go(g, flaky(1))
    assert result["status"] == "ok" and result["vars"]["r"]["resp"] == {"status": 200}
    # «Завершить» с ошибкой внутри подпрограммы — ошибка блока вызова
    g2 = chain(START, node("c", "call_scenario", scenario="bad", params=[]), AFTER)
    g2["nodes"] += [node("s3", "trigger", name="bad"), node("x", "stop", status="error", message="нет входа")]
    g2["edges"] += [edge("s3", "x")]
    result, _ = go(g2)
    assert result["status"] == "error" and "нет входа" in result["message"]


def test_call_rules_recursion_unknown_params():
    def two_way(a_calls, b_calls):
        g = {"name": "t", "kind": "automation", "nodes": [
            node("sa", "trigger", name="a"), node("ca", "call_scenario", scenario=a_calls, params=[]),
            node("sb", "trigger", name="b"), node("cb", "call_scenario", scenario=b_calls, params=[])],
            "edges": [edge("sa", "ca"), edge("sb", "cb")]}
        return g
    with pytest.raises(GraphError, match="по кругу"):
        compile_automation(two_way("b", "a"))
    with pytest.raises(GraphError, match="по кругу"):
        compile_automation(two_way("a", "a"))
    with pytest.raises(GraphError, match="сценария «nope» нет"):
        compile_automation(two_way("nope", "a"))
    g = chain(START, node("c", "call_scenario", scenario="sub", params=[{"key": "zzz", "expr": "1"}]))
    g["nodes"].append(node("s2", "trigger", name="sub", params=["user"]))
    with pytest.raises(GraphError, match="нет параметра «zzz»"):
        compile_automation(g)
    g["nodes"][1]["data"]["params"] = []
    assert any("не передан" in w.message for w in compile_automation(g).warnings)
    with pytest.raises(GraphError, match="выберите, какой сценарий"):
        compile_automation(chain(START, node("c", "call_scenario", scenario="", params=[])))


def test_old_agent_is_refused(tmp_path, monkeypatch):
    from fastapi.testclient import TestClient

    from app import main as platform_main, storage
    from dragapi_agent.agent import Agent
    from tests.test_automation import _connect_agent, _wait
    assert compile_automation(chain(START, http(retries="1"))).scenario("main").needs_new_agent
    assert not compile_automation(chain(START, http())).scenario("main").needs_new_agent

    monkeypatch.setattr(storage, "DATA_DIR", tmp_path / "projects")
    monkeypatch.setattr(storage, "DB_DIR", tmp_path / "db")
    old_hello = Agent.hello
    # агент старой версии: без возможности «retry»
    monkeypatch.setattr(Agent, "hello", lambda self: {**old_hello(self), "capabilities": ["input", "vision", "windows"]})
    with TestClient(platform_main.app, headers={"X-DragAPI": "1"}) as client:
        created = client.post("/api/agents", json={"name": "старый"}).json()
        pid = client.post("/api/projects", json={"name": "R", "kind": "automation"}).json()["id"]
        g = chain(START, node("w", "wait", seconds="0", on_error={"then": "skip"}))
        g["automation"] = {"agent": created["agent"]["id"]}
        client.put(f"/api/projects/{pid}", json=g)
        agent, ctx = _connect_agent(client, created["token"], FakeDriver())
        try:
            _wait(lambda: client.get("/api/agents").json()[0]["online"])
            r = client.post(f"/api/projects/{pid}/automation/run", json={"scenario": "main"})
            assert r.status_code == 409 and "агент устарел" in r.json()["detail"]
        finally:
            ctx.__exit__(None, None, None)
