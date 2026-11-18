"""Конструктор ботов: компиляция, тест-чат (эмулятор), живой бот через поддельный Telegram, связь со сценариями."""

import json
import threading
import time

import httpx
import pytest
from fastapi.testclient import TestClient

from app import bots, storage
from app import main as platform_main
from app.bots import compile_bot
from app.graph import GraphError
from dragapi_agent.driver import FakeDriver

from test_automation import FakeTelegram, _connect_agent, _wait, edge, node


def graph_form():
    """Анкета: /start → имя → возраст (целое, с повтором) → запомнить → меню → ветки."""
    return {"name": "Анкета", "kind": "bot",
            "nodes": [node("start", "bot_command", command="start", description="Начать"),
                      node("ask_name", "bot_ask", text="Как вас зовут?", var="name", type="text", timeout="60"),
                      node("ask_age", "bot_ask", text="Сколько вам лет?", var="age", type="int",
                           retry="Нужно целое число", timeout="60"),
                      node("save", "bot_save", assignments=[{"target": "name", "expr": "name"},
                                                            {"target": "age", "expr": "age"}]),
                      node("menu", "bot_menu", text="{name}, что дальше?", var="choice", timeout="60", remove=True,
                           buttons=[{"id": "a", "text": "Профиль"}, {"id": "b", "text": "Выход"}]),
                      node("prof", "bot_send", text="Вы {saved['name']}, {saved['age']} лет", links=[], chat=""),
                      node("bye", "bot_send", text="Пока!", links=[{"text": "Сайт", "url": "https://example.com"}],
                           chat=""),
                      node("late", "bot_send", text="Не дождался", links=[], chat=""),
                      node("hi", "bot_message", match="contains", pattern="привет", ignore_case=True),
                      node("hi_send", "bot_send", text="И вам привет, {user_name}", links=[], chat=""),
                      node("any", "bot_message", match="any"),
                      node("echo", "bot_send", text="Эхо: {text}", links=[], chat="")],
            "edges": [edge("start", "ask_name"), edge("ask_name", "ask_age", "ok"), edge("ask_age", "save", "ok"),
                      edge("save", "menu"), edge("menu", "prof", "a"), edge("menu", "bye", "b"),
                      edge("menu", "late", "timeout"), edge("hi", "hi_send"), edge("any", "echo")]}


def test_compile_routes_and_errors():
    p = compile_bot(graph_form())
    assert p.route("/start@MyBot")[0].node == "start"
    assert p.route("Привет всем")[0].node == "hi"          # «содержит» важнее «любого»
    assert p.route("что-то")[0].node == "any"
    assert {"kind": "command", "value": "start", "description": "Начать", "node": "start"} in p.summary()
    assert any("токен бота не задан" in w.message for w in p.warnings)

    g = graph_form()
    g["nodes"].append(node("dup", "bot_command", command="start"))
    g["nodes"].append(node("rx", "bot_message", match="regex", pattern="(", ignore_case=True))
    g["edges"].append(edge("menu", "late", "removed"))   # выход удалённой кнопки
    with pytest.raises(GraphError) as e:
        compile_bot(g)
    text = " | ".join(i.message for i in e.value.issues)
    assert "уже обрабатывается" in text and "регулярное выражение" in text and "неизвестного выхода" in text

    g = graph_form()
    g["nodes"][4]["data"]["buttons"] = []
    with pytest.raises(GraphError, match="хотя бы одну кнопку"):
        compile_bot(g)


@pytest.fixture
def platform(tmp_path, monkeypatch):
    monkeypatch.setattr(storage, "DATA_DIR", tmp_path / "projects")
    monkeypatch.setattr(storage, "DB_DIR", tmp_path / "db")
    with TestClient(platform_main.app, headers={"X-DragAPI": "1"}) as client:
        yield client


def test_emulator_dialog(platform):
    c = platform
    created = c.post("/api/projects", json={"name": "Анкета", "kind": "bot"}).json()
    pid = created["id"]
    assert created["kind"] == "bot" and created["nodes"][0]["type"] == "bot_command"
    assert c.post("/api/validate", json={**graph_form()}).json()["ok"]
    g = graph_form()

    def say(text=None, button=None, reset=False):
        r = c.post(f"/api/projects/{pid}/bot/emulator", json={"graph": g, "text": text, "button": button, "reset": reset})
        assert r.status_code == 200, r.text
        return r.json()

    def bot_texts(state):
        return [m["text"] for m in state["messages"] if m["from"] == "bot"]

    say(reset=True)
    st = say("/start")
    assert bot_texts(st)[-1] == "Как вас зовут?" and st["waiting"]
    st = say("Аня")
    assert bot_texts(st)[-1] == "Сколько вам лет?"
    st = say("двадцать")
    assert bot_texts(st)[-1] == "Нужно целое число"
    st = say("25")
    menu = st["messages"][-1]
    assert menu["text"] == "Аня, что дальше?" and [b["text"] for b in menu["buttons"]] == ["Профиль", "Выход"]
    assert st["saved"] == {"name": "Аня", "age": 25}
    st = say(button={"message_id": menu["id"], "data": "a", "text": "Профиль"})
    assert bot_texts(st)[-1] == "Вы Аня, 25 лет"
    assert next(m for m in st["messages"] if m["id"] == menu["id"])["buttons"] == []    # кнопки убраны
    # повторное нажатие старого меню — без последствий
    n = len(st["messages"])
    st = say(button={"message_id": menu["id"], "data": "b", "text": "Выход"})
    assert len([m for m in st["messages"] if m["from"] == "bot"]) == len([m for m in st["messages"][:n] if m["from"] == "bot"])
    # события «содержит» и «любое»
    assert bot_texts(say("ну привет"))[-1] == "И вам привет, Вы"
    assert bot_texts(say("123"))[-1] == "Эхо: 123"
    # команда прерывает незаконченный диалог; текст кнопки работает как нажатие
    say("/start")
    st = say("/start")
    assert bot_texts(st)[-2:] == ["Как вас зовут?", "Как вас зовут?"]
    say("Боб"); say("30")
    st = say("выход")
    assert bot_texts(st)[-1] == "Пока!" and st["messages"][-1]["links"] == [{"text": "Сайт", "url": "https://example.com"}]
    # ошибка в выражении видна в журнале и не роняет бота
    g["nodes"][9]["data"]["text"] = "{1 / 0}"
    st = say("привет")
    assert any(e["kind"] == "error" and "ZeroDivisionError" in e["text"] for e in st["log"])
    assert bot_texts(say("/start"))[-1] == "Как вас зовут?"


def test_live_bot_with_fake_telegram(platform, monkeypatch):
    from app import telegram as tg
    fake = FakeTelegram()
    monkeypatch.setattr(tg, "TRANSPORT", httpx.MockTransport(fake.handler))
    c = platform
    pid = c.post("/api/projects", json={"name": "Живой", "kind": "bot"}).json()["id"]
    g = {**graph_form(), "env": [{"key": "BOT_TOKEN", "value": "42:abc", "secret": True}], "bot": {"token_env": "BOT_TOKEN"}}
    assert c.put(f"/api/projects/{pid}", json=g).status_code == 200
    r = c.post(f"/api/projects/{pid}/bot/start")
    assert r.status_code == 200, r.text
    assert r.json()["running"] and r.json()["username"] == "dragapi_test_bot"
    _wait(lambda: any(m == "setMyCommands" for m, _ in fake.calls))
    assert next(b for m, b in fake.calls if m == "setMyCommands")["commands"][0]["command"] == "start"

    def user(text):
        fake.push({"message": {"chat": {"id": 7}, "from": {"id": 7, "first_name": "Ира"}, "text": text}})

    user("/start")
    _wait(lambda: fake.texts(7) and fake.texts(7)[-1] == "Как вас зовут?")
    user("Ира"); _wait(lambda: fake.texts(7)[-1] == "Сколько вам лет?")
    user("31"); menu = _wait(lambda: next((m for m in fake.sent if m.get("reply_markup")), None))
    rows = menu["reply_markup"]["inline_keyboard"]
    assert [r[0]["text"] for r in rows] == ["Профиль", "Выход"]
    fake.push({"callback_query": {"id": "q", "data": "a", "from": {"id": 7, "first_name": "Ира"},
                                  "message": {"message_id": fake.sent.index(menu) + 1, "chat": {"id": 7}}}})
    _wait(lambda: fake.texts(7)[-1] == "Вы Ира, 31 лет")
    assert any(m == "editMessageReplyMarkup" for m, _ in fake.calls)
    users = c.get(f"/api/projects/{pid}/bot/users").json()
    assert users[0]["name"] == "Ира" and users[0]["saved"] == {"name": "Ира", "age": 31}
    st = c.get(f"/api/projects/{pid}/bot/status").json()
    assert st["stats"]["in"] >= 4 and st["stats"]["out"] >= 4 and st["log"]

    # сохранение работающего бота подхватывается на лету
    g["nodes"][11]["data"]["text"] = "Новое эхо: {text}"
    c.put(f"/api/projects/{pid}", json=g)
    user("что угодно")
    _wait(lambda: fake.texts(7)[-1] == "Новое эхо: что угодно")

    # токен не может одновременно опрашивать автоматизация
    assert bots.manager.uses_token("42:abc")
    assert c.post(f"/api/projects/{pid}/bot/stop").json()["running"] is False
    assert not bots.manager.live
    # без токена — понятная ошибка
    pid2 = c.post("/api/projects", json={"name": "Без токена", "kind": "bot"}).json()["id"]
    r = c.post(f"/api/projects/{pid2}/bot/start")
    assert r.status_code == 409 and "токен" in r.json()["detail"]["message"]


def test_bot_runs_automation_scenario(platform):
    c = platform
    created = c.post("/api/agents", json={"name": "ПК"}).json()
    apid = c.post("/api/projects", json={"name": "Клики", "kind": "automation"}).json()["id"]
    c.put(f"/api/projects/{apid}", json={
        "name": "Клики", "kind": "automation", "automation": {"agent": created["agent"]["id"], "enabled": True},
        "nodes": [node("t", "trigger", name="click", kind="webhook", params=["x"]),
                  node("m", "mouse_click", x="x", y="10"),
                  node("v", "set_vars", assignments=[{"target": "done", "expr": "x * 2"}])],
        "edges": [edge("t", "m"), edge("m", "v")]})
    bpid = c.post("/api/projects", json={"name": "Пульт", "kind": "bot"}).json()["id"]
    g = {"name": "Пульт", "kind": "bot",
         "nodes": [node("go", "bot_command", command="go"),
                   node("run", "run_scenario", project=apid, scenario="click", params=[{"key": "x", "expr": "int(args)"}],
                        wait=True, target="res"),
                   node("ans", "bot_send", text="{res['status']}: {res['vars']['done']}", links=[], chat="")],
         "edges": [edge("go", "run"), edge("run", "ans")]}
    driver = FakeDriver()
    agent, ctx = _connect_agent(c, created["token"], driver)
    try:
        _wait(lambda: c.get("/api/agents").json()[0]["online"])
        st = c.post(f"/api/projects/{bpid}/bot/emulator", json={"graph": g, "text": "/go 21", "reset": True}).json()
        texts = [m["text"] for m in st["messages"] if m["from"] == "bot"]
        assert texts == ["ok: 42"], st
        assert ("click", "left", 1, 21, 10) in driver.actions
        runs = c.get(f"/api/projects/{apid}/automation/runs").json()
        assert runs[0]["source"] == "bot"
    finally:
        ctx.__exit__(None, None, None)
