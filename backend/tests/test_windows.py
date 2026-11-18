"""Окна: «Найти окно», «Окно: действие», клики и поиск «внутри окна»."""

import pytest
from fastapi.testclient import TestClient

from app import main as platform_main, storage
from app.automation import compile_automation
from app.graph import GraphError
from dragapi_agent.driver import FakeDriver
from dragapi_agent.windows import at_point, matches
from tests.test_automation import _connect_agent, _wait, chain, edge, node, run
from tests.test_vision import BUTTON, SCREEN


def windows():
    return [
        {"id": 1, "title": "Документ — Блокнот", "process": "notepad.exe", "pid": 10, "x": 100, "y": 80, "w": 600,
         "h": 400, "state": "normal", "active": False},
        {"id": 2, "title": "Mozilla Firefox", "process": "firefox", "pid": 11, "x": 0, "y": 0, "w": 1920, "h": 1040,
         "state": "maximized", "active": True},
    ]


def driver():
    d = FakeDriver()
    d.windows = windows()
    return d


def find(**data):
    return node("w", "window_find", **{"title": "блокнот", "wait": "0", "target": "win", **data})


def with_branches(g, src="w"):
    g["nodes"] += [node("yes", "log", message="есть {win['title']}"), node("no", "log", message="нет окна")]
    g["edges"] += [edge(src, "yes", "true"), edge(src, "no", "false")]
    return g


def logs(events):
    return [e["message"] for e in events if e.get("type") == "log"]


def start():
    return node("s", "trigger", name="main", kind="manual")


def test_find_activates_and_stores_window():
    result, d, events = run(with_branches(chain(start(), find())), driver=driver())
    assert result["status"] == "ok", result
    win = result["vars"]["win"]
    assert win["found"] and win["id"] == 1 and win["active"] and (win["x"], win["y"], win["w"], win["h"]) == (100, 80, 600, 400)
    assert ("window", "activate", 1) in d.actions and "есть Документ — Блокнот" in logs(events)


def test_find_by_process_regex_and_not_found():
    result, d, _ = run(with_branches(chain(start(), find(title="", process="firefox", activate=False))), driver=driver())
    assert result["vars"]["win"]["id"] == 2 and not [a for a in d.actions if a[0] == "window"]
    result, _, _ = run(with_branches(chain(start(), find(title=r"^Mozilla", match="regex"))), driver=driver())
    assert result["vars"]["win"]["id"] == 2
    result, _, events = run(with_branches(chain(start(), find(title="Калькулятор"))), driver=driver())
    assert result["vars"]["win"] == {"found": False} and "нет окна" in logs(events)
    assert matches({"title": "A", "process": "Code"}, "", process="code")


def test_wait_until_window_closes():
    d = driver()
    calls = {"n": 0}
    orig = d.list_windows

    def closing():                   # блокнот закрывается к третьей проверке
        calls["n"] += 1
        if calls["n"] == 3:
            d.windows = [w for w in d.windows if w["id"] != 1]
        return orig()

    d.list_windows = closing
    result, _, events = run(with_branches(chain(start(), find(mode="disappear", wait="5"))), driver=d)
    assert calls["n"] == 3 and logs(events)[0].startswith("окно «блокнот» закрылось")


def test_click_inside_window_follows_it():
    """Окно сдвинули после «Найти окно» — клик всё равно попадает в нужное место окна."""
    d = driver()
    g = chain(start(), find(), node("c", "mouse_click", x="10", y="20", window="win"))
    g["edges"] = [edge("s", "w"), edge("w", "c", "true")]
    d_actions = []

    def moved_after_find(wid):      # к моменту клика окно уехало
        w = next(w for w in d.windows if w["id"] == wid)
        return {**w, "x": 300, "y": 200} if any(a[0] == "window" for a in d.actions) else dict(w)

    d.get_window = moved_after_find
    result, d, _ = run(g, driver=d)
    assert result["status"] == "ok", result
    assert ("click", "left", 1, 310, 220) in d.actions
    assert result["vars"]["win"]["x"] == 300           # переменная обновилась


def test_window_action_move_minimize_close():
    def scenario(action, **data):
        g = chain(start(), find(), node("a", "window_action", window="win", action=action, **data))
        g["edges"] = [edge("s", "w"), edge("w", "a", "true")]
        return g

    result, d, events = run(scenario("move", x="10", y="20", w="800", h=""), driver=driver())
    assert result["status"] == "ok", result
    win = result["vars"]["win"]
    assert (win["x"], win["y"], win["w"], win["h"]) == (10, 20, 800, 400) and "перемещено" in logs(events)[-1]
    result, d, _ = run(scenario("minimize"), driver=driver())
    assert result["vars"]["win"]["state"] == "minimized"
    result, d, events = run(scenario("close"), driver=driver())
    assert result["vars"]["win"]["closed"] and all(w["id"] != 1 for w in d.windows) and logs(events)[-1] == "окно закрыто"
    result, _, _ = run(scenario("move", x="", y="", w="", h=""), driver=driver())
    assert result["status"] == "error" and "положение и/или размер" in result["message"]


def test_closed_or_minimized_window_is_clear_error():
    d = driver()
    g = chain(start(), find(activate=False), node("a", "window_action", window="win", action="minimize"),
              node("c", "mouse_click", x="5", y="5", window="win"))
    g["edges"] = [edge("s", "w"), edge("w", "a", "true"), edge("a", "c")]
    result, _, _ = run(g, driver=d)
    assert result["status"] == "error" and "свёрнуто" in result["message"]
    g = chain(start(), node("c", "mouse_click", x="5", y="5", window="win"))
    result, _, _ = run(g, driver=driver())
    assert result["status"] == "error" and "сначала блок «Найти окно»" in result["message"]


def test_image_search_inside_window():
    d = driver()
    d.screen = SCREEN
    d.windows[0].update(x=400, y=250, w=300, h=200)       # кнопка (500,300) внутри окна
    g = chain(start(), find(), node("f", "image_find", image=BUTTON, wait="0", window="win", action="none"))
    g["edges"] = [edge("s", "w"), edge("w", "f", "true")]
    result, _, _ = run(g, driver=d)
    assert result["vars"]["found"]["found"] and result["vars"]["found"]["x"] == 540
    d = driver()
    d.screen = SCREEN
    d.windows[0].update(x=0, y=0, w=300, h=200)           # кнопка вне окна — не найдена
    result, _, _ = run(g, driver=d)
    assert not result["vars"]["found"]["found"]


def test_compile_window_rules():
    with pytest.raises(GraphError, match="заголовок окна или программу"):
        compile_automation(chain(start(), find(title="", process="")))
    with pytest.raises(GraphError, match="регулярное выражение"):
        compile_automation(chain(start(), find(title="(", match="regex")))
    with pytest.raises(GraphError, match="укажите переменную"):
        compile_automation(chain(start(), node("a", "window_action", window="", action="activate")))
    with pytest.raises(GraphError, match="Внутри окна"):
        compile_automation(chain(start(), node("c", "mouse_click", x="1", y="1", window="1win")))
    plan = compile_automation(chain(start(), node("o", "ocr", window="win"))).scenario("main").plan()
    assert plan["steps"][0]["window"] == "win" and plan["steps"][0]["region"] is None


def test_at_point():
    wins = windows()
    assert at_point(wins, 150, 100)["id"] == 1 and at_point(wins, 1000, 900)["id"] == 2
    wins[0]["state"] = "minimized"
    assert at_point(wins, 150, 100)["id"] == 2


def test_agent_lists_windows_and_point_window(tmp_path, monkeypatch):
    monkeypatch.setattr(storage, "DATA_DIR", tmp_path / "projects")
    monkeypatch.setattr(storage, "DB_DIR", tmp_path / "db")
    with TestClient(platform_main.app, headers={"X-DragAPI": "1"}) as client:
        created = client.post("/api/agents", json={"name": "ПК"}).json()
        d = driver()
        d.pos = (130, 110)
        agent, ctx = _connect_agent(client, created["token"], d)
        try:
            aid = created["agent"]["id"]
            _wait(lambda: client.get("/api/agents").json()[0]["online"])
            pid = client.post("/api/projects", json={"name": "Окна", "kind": "automation"}).json()["id"]
            req = client.post(f"/api/projects/{pid}/automation/windows", json={"agent": aid}).json()["id"]
            res = _wait(lambda: (x := client.get(f"/api/projects/{pid}/automation/request/{req}").json()).get("done") and x)
            assert [w["title"] for w in res["items"]] == ["Документ — Блокнот", "Mozilla Firefox"]
            p = client.post(f"/api/projects/{pid}/automation/pick-point", json={"agent": aid, "countdown": 0}).json()
            pt = _wait(lambda: (x := client.get(f"/api/projects/{pid}/automation/pick-point/{p['id']}").json()).get("x") is not None and x)
            assert (pt["x"], pt["y"]) == (130, 110) and pt["window"]["id"] == 1
            # «Проверить» для «Найти окно» — без активации
            graph = {**with_branches(chain(start(), find())), "automation": {"agent": aid}}
            r = client.post(f"/api/projects/{pid}/automation/vision-test", json={"agent": aid, "node": "w", "graph": graph})
            res = _wait(lambda: (x := client.get(f"/api/projects/{pid}/automation/request/{r.json()['id']}").json()).get("done") and x)
            assert res["status"] == "ok" and res["vars"]["win"]["id"] == 1 and "окно «Документ — Блокнот»" in res["message"]
            assert not [a for a in d.actions if a[0] == "window"]
        finally:
            ctx.__exit__(None, None, None)
