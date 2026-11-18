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
    assert {"kind": "command", "value": "start", "description": "Начать", "node": "start", "roles": []} in p.summary()
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


def graph_admin():
    """Роли: /panel — только админам и модераторам, /mod @user — назначить модератора, /ban, рассылка, оплата."""
    return {"name": "Админка", "kind": "bot",
            "bot": {"roles": ["moderator", "vip"], "admins": ["500"], "deny_text": "Только для своих"},
            "nodes": [node("start", "bot_command", command="start"),
                      node("hello", "bot_send", text="Роль: {role}, админ: {is_admin}", links=[], to="current"),
                      node("panel", "bot_command", command="panel", roles=["moderator"]),
                      node("panel_send", "bot_send", text="Панель", links=[], to="current"),
                      node("mod", "bot_command", command="mod", roles=["admin"]),
                      node("set", "bot_role", target="{args}", role="moderator", var="who"),
                      node("ok", "bot_send", text="{who['name']} теперь {who['role']}", links=[], to="current"),
                      node("nf", "bot_send", text="Не нашёл {args}", links=[], to="current"),
                      node("ban", "bot_command", command="ban", roles=["admin", "moderator"]),
                      node("setban", "bot_role", target="{args}", role="banned", var=""),
                      node("news", "bot_command", command="news", roles=["admin"]),
                      node("bc", "bot_send", text="Новость: {args}", links=[], to="role", role="all", count_var="n"),
                      node("bc_done", "bot_send", text="Отправлено: {n}", links=[], to="current"),
                      node("buy", "bot_command", command="buy"),
                      node("inv", "bot_invoice", title="VIP", description="Доступ", amount="25", currency="XTR",
                           timeout="30", var="payment"),
                      node("vip", "bot_role", target="", role="vip", var=""),
                      node("thanks", "bot_send", text="Спасибо! {payment['amount']} ⭐, теперь вы {role}", links=[], to="current"),
                      node("unpaid", "bot_send", text="Не оплачено", links=[], to="current")],
            "edges": [edge("start", "hello"), edge("panel", "panel_send"), edge("mod", "set"),
                      edge("set", "ok", "ok"), edge("set", "nf", "not_found"), edge("ban", "setban"),
                      edge("news", "bc"), edge("bc", "bc_done"), edge("buy", "inv"),
                      edge("inv", "vip", "paid"), edge("vip", "thanks", "ok"), edge("inv", "unpaid", "timeout")]}


def test_roles_compile_rules():
    g = graph_admin()
    p = compile_bot(g)
    assert p.commands["panel"].roles == ["moderator"] and p.all_roles() == ["admin", "moderator", "vip", "user", "banned"]
    g["nodes"][2]["data"]["roles"] = ["ghost"]
    g["nodes"][14]["data"]["currency"] = "RUB"
    g["bot"]["admins"] = ["abc"]
    with pytest.raises(GraphError) as e:
        compile_bot(g)
    text = " | ".join(i.message for i in e.value.issues)
    assert "неизвестная роль «ghost»" in text and "провайдера" in text and "ID администратора" in text


def test_live_bot_roles_broadcast_payments(platform, monkeypatch):
    from app import telegram as tg
    fake = FakeTelegram()
    monkeypatch.setattr(tg, "TRANSPORT", httpx.MockTransport(fake.handler))
    c = platform
    pid = c.post("/api/projects", json={"name": "Админка", "kind": "bot"}).json()["id"]
    g = {**graph_admin(), "env": [{"key": "BOT_TOKEN", "value": "1:x", "secret": True}],
         "bot": {**graph_admin()["bot"], "token_env": "BOT_TOKEN"}}
    assert c.put(f"/api/projects/{pid}", json=g).status_code == 200
    assert c.post(f"/api/projects/{pid}/bot/start").status_code == 200

    names = {7: "ira", 8: "oleg"}

    def msg(uid, name, text, username=None):
        username = username or names.get(uid)
        fake.push({"message": {"chat": {"id": uid}, "from": {"id": uid, "first_name": name, "username": username},
                               "text": text}})

    def last(uid):
        return _wait(lambda: fake.texts(uid) and fake.texts(uid)[-1])

    msg(500, "Админ", "/start"); assert last(500) == "Роль: admin, админ: True"
    msg(7, "Ира", "/start", "ira"); assert last(7) == "Роль: user, админ: False"
    msg(8, "Олег", "/start", "oleg"); assert last(8) == "Роль: user, админ: False"
    msg(7, "Ира", "/panel"); _wait(lambda: fake.texts(7)[-1] == "Только для своих")
    msg(7, "Ира", "/mod @oleg"); _wait(lambda: len(fake.texts(7)) == 3 and fake.texts(7)[-1] == "Только для своих")
    # админ делает Иру модератором по @username
    msg(500, "Админ", "/mod @ira"); _wait(lambda: fake.texts(500)[-1] == "Ира теперь moderator")
    msg(500, "Админ", "/mod @nobody"); _wait(lambda: fake.texts(500)[-1] == "Не нашёл @nobody")
    msg(7, "Ира", "/panel"); _wait(lambda: fake.texts(7)[-1] == "Панель")
    # меню команд для ролей — только в их чатах
    _wait(lambda: any(b.get("scope", {}).get("chat_id") == "7" for m, b in fake.calls if m == "setMyCommands"))
    scoped = [b for m, b in fake.calls if m == "setMyCommands" and (b.get("scope") or {}).get("chat_id") == "7"][-1]
    assert {c_["command"] for c_ in scoped["commands"]} >= {"panel", "ban", "start", "buy"} and \
        "mod" not in {c_["command"] for c_ in scoped["commands"]}
    public = [b for m, b in fake.calls if m == "setMyCommands" and not b.get("scope")][-1]
    assert {c_["command"] for c_ in public["commands"]} == {"start", "buy"}
    # модератор банит Олега — бот его больше не слышит
    msg(7, "Ира", "/ban 8"); _wait(lambda: c.get(f"/api/projects/{pid}/bot/users").json() and
                                    any(u["id"] == "8" and u.get("role") == "banned"
                                        for u in c.get(f"/api/projects/{pid}/bot/users").json()))
    n = len(fake.texts(8))
    msg(8, "Олег", "/start"); time.sleep(0.5)
    assert len(fake.texts(8)) == n
    # рассылка всем (кроме заблокированных)
    msg(500, "Админ", "/news Завтра акция"); _wait(lambda: fake.texts(500)[-1] == "Отправлено: 2")
    assert fake.texts(7)[-1] == "Новость: Завтра акция" and fake.texts(8)[-1] != "Новость: Завтра акция"

    # оплата звёздами: счёт → pre_checkout (суммы сверяются) → successful_payment → роль vip
    msg(7, "Ира", "/buy")
    inv = _wait(lambda: next((b for m, b in fake.calls if m == "sendInvoice"), None))
    assert inv["currency"] == "XTR" and inv["prices"][0]["amount"] == 25 and inv["provider_token"] == ""
    fake.push({"pre_checkout_query": {"id": "bad", "from": {"id": 7, "first_name": "Ира"}, "currency": "XTR",
                                      "total_amount": 1, "invoice_payload": inv["payload"]}})
    _wait(lambda: any(m == "answerPreCheckoutQuery" and b["pre_checkout_query_id"] == "bad" for m, b in fake.calls))
    assert next(b for m, b in fake.calls if m == "answerPreCheckoutQuery" and b["pre_checkout_query_id"] == "bad")["ok"] is False
    fake.push({"pre_checkout_query": {"id": "good", "from": {"id": 7, "first_name": "Ира"}, "currency": "XTR",
                                      "total_amount": 25, "invoice_payload": inv["payload"]}})
    _wait(lambda: any(m == "answerPreCheckoutQuery" and b["pre_checkout_query_id"] == "good" and b["ok"] for m, b in fake.calls))
    fake.push({"message": {"chat": {"id": 7}, "from": {"id": 7, "first_name": "Ира"}, "successful_payment": {
        "currency": "XTR", "total_amount": 25, "invoice_payload": inv["payload"], "telegram_payment_charge_id": "ch1"}}})
    _wait(lambda: fake.texts(7)[-1] == "Спасибо! 25 ⭐, теперь вы vip")
    pays = c.get(f"/api/projects/{pid}/bot/payments").json()
    assert pays[0]["amount"] == 25 and pays[0]["charge_id"] == "ch1" and pays[0]["user_id"] == "7"

    # роль из платформы
    r = c.put(f"/api/projects/{pid}/bot/users/8", json={"role": "user"})
    assert r.status_code == 200 and "role" not in r.json()
    assert c.put(f"/api/projects/{pid}/bot/users/500", json={"role": "user"}).status_code == 400
    assert c.put(f"/api/projects/{pid}/bot/users/8", json={"role": "ghost"}).status_code == 400
    c.post(f"/api/projects/{pid}/bot/stop")


def test_emulator_roles_and_test_payment(platform):
    c = platform
    pid = c.post("/api/projects", json={"name": "Э", "kind": "bot"}).json()["id"]
    g = graph_admin()

    def say(**kw):
        r = c.post(f"/api/projects/{pid}/bot/emulator", json={"graph": g, **kw})
        assert r.status_code == 200, r.text
        return r.json()

    st = say(reset=True)
    assert st["role"] == "user" and "vip" in st["roles"]
    assert say(text="/panel")["messages"][-1]["text"] == "Только для своих"
    st = say(text="/panel", as_role="moderator")
    assert st["messages"][-1]["text"] == "Панель" and st["role"] == "moderator"
    st = say(text="/buy", as_role="user")
    inv = st["messages"][-1]
    assert inv["invoice"]["amount"] == 25 and "25 ⭐" in inv["text"]
    st = say(pay={"message_id": inv["id"]})
    assert st["messages"][-1]["text"] == "Спасибо! 25 ⭐, теперь вы vip" and st["role"] == "vip"


def graph_group():
    """Модерация группы, файлы, контакт, клавиатура, возврат."""
    return {"name": "Модератор", "kind": "bot", "bot": {"roles": ["moderator"], "admins": []},
            "nodes": [
                # приветствие новичков
                node("join", "bot_member", action="join"),
                node("welcome", "bot_send", text="Добро пожаловать, {user_name}!", links=[], to="current"),
                # антиспам на любое сообщение в группах
                node("any", "bot_message", match="any", where="group"),
                node("spam", "bot_filter", links=True, words=["казино"], caps=True, max_length="0",
                     flood_count="3", flood_seconds="60", skip_admins=True, var="reason"),
                node("del", "bot_moderate", action="delete", target="author", minutes="0", var="err"),
                node("warn", "bot_send", text="{user_name}, удалено: {reason}", links=[], to="current"),
                # /mute ответом на сообщение — только админам группы
                node("mute", "bot_command", command="mute", where="group"),
                node("chk", "condition", expr="is_chat_admin"),
                node("do_mute", "bot_moderate", action="mute", target="reply", minutes="30", var="err"),
                node("muted", "bot_send", text="{reply_user_name} помолчит 30 минут", links=[], to="current"),
                node("mute_err", "bot_send", text="Ошибка: {err}", links=[], to="current"),
                node("noadm", "bot_send", text="Только для админов группы", links=[], to="current"),
                # личка: фото → переслать админу; контакт; клавиатура
                node("photo", "bot_file", kind="photo", where="private"),
                node("fwd", "bot_send_file", source="file", caption="от {user_name}", to="chat", chat="999"),
                node("got", "bot_send", text="Фото {file['name']} получено", links=[], to="current"),
                node("phone", "bot_command", command="phone", where="private"),
                node("ask", "bot_ask", text="Ваш номер?", var="tel", type="contact", timeout="30",
                     retry="Нажмите кнопку", button="📱 Номер"),
                node("tel_ok", "bot_send", text="Номер {tel}", links=[], to="current"),
                node("kb", "bot_command", command="kb", where="private"),
                node("menu", "bot_menu", text="Выберите", keyboard="reply", columns="2", var="c", timeout="30",
                     remove=True, buttons=[{"id": "x", "text": "Да"}, {"id": "y", "text": "Нет"}]),
                node("yes", "bot_send", text="Выбрано: {c}", links=[], to="current")],
            "edges": [edge("join", "welcome"), edge("any", "spam"), edge("spam", "del", "violation"),
                      edge("del", "warn", "ok"), edge("mute", "chk"), edge("chk", "do_mute", "true"),
                      edge("chk", "noadm", "false"), edge("do_mute", "muted", "ok"), edge("do_mute", "mute_err", "error"),
                      edge("photo", "fwd"), edge("fwd", "got"), edge("phone", "ask"), edge("ask", "tel_ok", "ok"),
                      edge("kb", "menu"), edge("menu", "yes", "x")]}


def test_emulator_group_files_keyboard(platform):
    c = platform
    pid = c.post("/api/projects", json={"name": "М", "kind": "bot"}).json()["id"]
    g = graph_group()

    def say(**kw):
        r = c.post(f"/api/projects/{pid}/bot/emulator", json={"graph": g, **kw})
        assert r.status_code == 200, r.text
        return r.json()

    def bot(st):
        return [m["text"] for m in st["messages"] if m["from"] in ("bot", "system")]

    say(reset=True)
    # вход в группу
    assert bot(say(member="join", group=True, as_other=True))[-1] == "Добро пожаловать, Участник!"
    # спам удаляется, причина видна
    st = say(text="Заходите на casino.ru", group=True, as_other=True)
    spam = [m for m in st["messages"] if m["from"] == "user"][-1]
    assert spam["deleted"] and bot(st)[-1] == "Участник, удалено: ссылка"
    assert bot(say(text="ВСЕ СЮДА БЫСТРО СМОТРИТЕ", group=True, as_other=True))[-1] == "Участник, удалено: капс"
    ok = say(text="обычное сообщение", group=True, as_other=True)
    target = [m for m in ok["messages"] if m["from"] == "user"][-1]
    assert not target.get("deleted")
    # /mute ответом: не админ группы — отказ; админ — мьют
    assert bot(say(text="/mute", group=True, reply_to=target["id"]))[-1] == "Только для админов группы"
    st = say(text="/mute", group=True, reply_to=target["id"], chat_admin=True)
    assert "участник 2 замьючен до" in bot(st)[-2] and bot(st)[-1] == "Участник помолчит 30 минут"
    st = say(text="/mute", group=True, chat_admin=True)
    assert "ответом на сообщение" in bot(st)[-1]
    # админ группы антиспамом не проверяется
    st = say(text="КАПС ОТ АДМИНА ГРУППЫ ТУТ", group=True)
    assert not [m for m in st["messages"] if m["from"] == "user"][-1].get("deleted")
    # флуд: больше 3 сообщений за минуту
    for i in range(3):
        say(text=f"раз {i}", group=True, as_other=True)
    assert "флуд" in bot(say(text="ещё", group=True, as_other=True))[-1]
    # в личке: фото пересылается админу (в чат 999), команда группы в личке не работает
    st = say(file={"name": "cat.png", "mime": "image/png", "size": 1234, "preview": "data:image/png;base64,AAAA"})
    fwd = [m for m in st["messages"] if m["from"] == "bot" and m.get("media")][-1]
    assert fwd["chat"] == "999" and fwd["media"]["kind"] == "photo" and fwd["text"] == "от Вы"
    assert bot(st)[-1] == "Фото cat.png получено"
    n = len(bot(st))
    assert len(bot(say(text="/mute"))) == n
    # контакт кнопкой
    st = say(text="/phone")
    assert st["keyboard"] == [[{"text": "📱 Номер", "request_contact": True}]]
    assert bot(say(text="не скажу"))[-1] == "Нажмите кнопку"
    st = say(contact=True)
    assert bot(st)[-1] == "Номер +79000000000" and st["keyboard"] is None
    # клавиатура под полем ввода: ответ приходит текстом кнопки
    st = say(text="/kb")
    assert st["keyboard"] == [[{"text": "Да"}, {"text": "Нет"}]]
    assert bot(say(text="Да"))[-1] == "Выбрано: Да"


def test_live_group_moderation_and_refund(platform, monkeypatch):
    from app import telegram as tg
    fake = FakeTelegram()
    statuses = {}
    orig = fake.handler

    async def handler(request):
        method = request.url.path.rsplit("/", 1)[-1]
        if method == "getChatMember":
            body = json.loads(request.content)
            fake.calls.append((method, body))
            return httpx.Response(200, json={"ok": True, "result": {"status": statuses.get(body["user_id"], "member")}})
        return await orig(request)

    monkeypatch.setattr(tg, "TRANSPORT", httpx.MockTransport(handler))
    c = platform
    pid = c.post("/api/projects", json={"name": "Группа", "kind": "bot"}).json()["id"]
    g = {**graph_group(), "env": [{"key": "BOT_TOKEN", "value": "5:x", "secret": True}],
         "bot": {**graph_group()["bot"], "token_env": "BOT_TOKEN"}}
    assert c.put(f"/api/projects/{pid}", json=g).status_code == 200
    assert c.post(f"/api/projects/{pid}/bot/start").status_code == 200
    group = {"id": -500, "type": "supergroup", "title": "Чат"}

    def gmsg(uid, name, text, mid, reply=None):
        m = {"message_id": mid, "chat": group, "from": {"id": uid, "first_name": name}, "text": text}
        if reply:
            m["reply_to_message"] = reply
        fake.push({"message": m})

    fake.push({"message": {"message_id": 1, "chat": group, "from": {"id": 9, "first_name": "Новичок"},
                           "new_chat_members": [{"id": 9, "first_name": "Новичок"}]}})
    _wait(lambda: fake.texts(-500) and fake.texts(-500)[-1] == "Добро пожаловать, Новичок!")
    gmsg(9, "Новичок", "купи в казино", 2)
    _wait(lambda: any(m == "deleteMessage" and b == {"chat_id": "-500", "message_id": 2} for m, b in fake.calls))
    _wait(lambda: fake.texts(-500)[-1] == "Новичок, удалено: стоп-слово «казино»")
    statuses[1] = "administrator"
    gmsg(1, "Админ", "/mute@bot", 3, reply={"message_id": 2, "from": {"id": 9, "first_name": "Новичок"}})
    rc = _wait(lambda: next((b for m, b in fake.calls if m == "restrictChatMember"), None))
    assert rc["user_id"] == 9 and rc["permissions"]["can_send_messages"] is False and rc["until_date"] > time.time() + 1700
    _wait(lambda: fake.texts(-500)[-1] == "Новичок помолчит 30 минут")

    # личка: фото → sendPhoto в чат 999 по file_id
    fake.push({"message": {"message_id": 4, "chat": {"id": 9, "type": "private"}, "from": {"id": 9, "first_name": "Новичок"},
                           "photo": [{"file_id": "small", "file_size": 10}, {"file_id": "big", "file_size": 999}],
                           "caption": "смотри"}})
    sp = _wait(lambda: next((b for m, b in fake.calls if m == "sendPhoto"), None))
    assert sp == {"chat_id": "999", "photo": "big", "caption": "от Новичок"}

    # возврат Stars из «Платежей»
    bots.manager.store(pid).add_payment({"t": time.time(), "user_id": "9", "amount": 10, "currency": "XTR",
                                         "charge_id": "ch-9", "payload": "p"})
    assert c.post(f"/api/projects/{pid}/bot/payments/refund", json={"charge_id": "ch-9"}).json() == {"ok": True}
    assert next(b for m, b in fake.calls if m == "refundStarPayment") == {"user_id": 9, "telegram_payment_charge_id": "ch-9"}
    assert c.get(f"/api/projects/{pid}/bot/payments").json()[0]["refunded"]
    assert c.post(f"/api/projects/{pid}/bot/payments/refund", json={"charge_id": "ch-9"}).status_code == 409
    c.post(f"/api/projects/{pid}/bot/stop")


# --- шаблоны, отложенные ветки, расписание, скачивание, статистика, выгрузка ---

from app import bot_templates


@pytest.mark.parametrize("tid", list(bot_templates.TEMPLATES))
def test_templates_compile(tid):
    g = {"name": tid, "kind": "bot", **bot_templates.build(tid)}
    p = compile_bot(g)
    assert p.commands or p.messages or p.members
    assert not [w for w in p.warnings if "не подключён" in w.message], [w.message for w in p.warnings]
    ids = [n["id"] for n in g["nodes"]]
    assert len(set((n["position"]["x"], n["position"]["y"]) for n in g["nodes"])) == len(ids)   # блоки не наложены


def test_templates_in_emulator(platform):
    c = platform
    assert {t["id"] for t in c.get("/api/bots/templates").json()} == set(bot_templates.TEMPLATES)
    assert c.post("/api/projects", json={"name": "x", "kind": "bot", "template": "nope"}).status_code == 400

    def make(tid):
        p = c.post("/api/projects", json={"name": tid, "kind": "bot", "template": tid}).json()
        assert p["nodes"] and p["kind"] == "bot"
        return p["id"]

    def say(pid, **kw):
        r = c.post(f"/api/projects/{pid}/bot/emulator", json=kw)
        assert r.status_code == 200, r.text
        return r.json()

    def bot(st):
        return [m["text"] for m in st["messages"] if m["from"] in ("bot", "system")]

    # запись на приём: услуга → имя → дата → телефон → подтверждение → напоминание (в тест-чате через ~3 с)
    pid = make("booking")
    say(pid, reset=True)
    st = say(pid, text="/start")
    menu = st["messages"][-1]
    st = say(pid, button={"message_id": menu["id"], "data": "haircut", "text": "Стрижка"})
    assert bot(st)[-1] == "Как к вам обращаться?"
    say(pid, text="Аня")
    st = say(pid, text="пятница, 15:00")
    assert st["keyboard"][0][0]["request_contact"]
    st = say(pid, contact=True)
    assert any("Вы записаны: Стрижка, пятница, 15:00" in t for t in bot(st))
    assert any("отложено на 1 дн." in t for t in bot(st))
    st = _wait(lambda: (lambda s: s if any(t.startswith("Напоминаем о записи: Стрижка") for t in bot(s)) else None)(
        c.get(f"/api/projects/{pid}/bot/emulator").json()), timeout=8)
    assert bot(say(pid, text="/my"))[-1] == "Ваша запись: Стрижка, пятница, 15:00"
    say(pid, text="/cancel")
    assert bot(say(pid, text="/my"))[-1] == "Записей нет. Записаться — /start"
    # воронка тест-чата: до каждого шага дошли, счётчики есть
    stats = c.get(f"/api/projects/{pid}/bot/stats?source=emulator").json()
    assert stats["nodes"]["start"] == 1 and stats["nodes"]["confirm"] == 1 and stats["commands"][0][0] in ("/my", "/start")

    # магазин: товар → счёт Stars → оплата → /orders
    pid = make("shop")
    say(pid, reset=True)
    menu = say(pid, text="/start")["messages"][-1]
    st = say(pid, button={"message_id": menu["id"], "data": "mug", "text": "Кружка"})
    inv = st["messages"][-1]
    assert inv["invoice"]["amount"] == 30 and inv["invoice"]["title"] == "Кружка"
    st = say(pid, pay={"message_id": inv["id"]})
    assert any(t.startswith("Спасибо! Заказ «Кружка» оплачен (30 ⭐)") for t in bot(st))
    assert bot(say(pid, text="/orders"))[-1] == "Покупок: 1. Последняя: Кружка"

    # анкета: клавиатура под полем ввода, расписание запускается вручную
    pid = make("survey")
    say(pid, reset=True)
    say(pid, text="/start"); say(pid, text="Олег"); st = say(pid, text="сорок")
    assert bot(st)[-1] == "Напишите возраст числом, например 30"
    st = say(pid, text="40")
    assert st["keyboard"] == [[{"text": "От друзей"}, {"text": "Поиск"}], [{"text": "Соцсети"}, {"text": "Другое"}]]
    st = say(pid, text="Поиск")
    assert bot(st)[-1] == "Спасибо, Олег! Анкета сохранена." and st["saved"]["source"] == "Поиск"
    st = say(pid, run_schedule="remind")
    assert "по расписанию" in bot(st)[-1] or "расписани" in " ".join(bot(st))

    # модератор: спам удаляется
    pid = make("moderator")
    say(pid, reset=True)
    st = say(pid, text="Покупайте на shop.ru", group=True, as_other=True)
    assert [m for m in st["messages"] if m["from"] == "user"][-1]["deleted"]


def test_delay_persisted_and_schedule_tick(tmp_path):
    import asyncio
    from app.bots import BotEngine, BotStore, EmulatorTransport
    g = {"name": "s", "kind": "bot",
         "nodes": [node("sch", "bot_schedule", kind="interval", every_minutes="1", at="10:00", weekdays=[]),
                   node("bc", "bot_send", text="Отчёт {now}", links=[], to="role", role="all", count_var="n"),
                   node("c", "bot_command", command="later"),
                   node("d", "bot_delay", amount="2", unit="hours"),
                   node("now", "bot_send", text="Напомню", links=[], to="current"),
                   node("lt", "bot_send", text="Пора, {user_name}!", links=[], to="current")],
         "edges": [edge("sch", "bc"), edge("c", "d"), edge("d", "now", "now"), edge("d", "lt", "later")]}
    compiled = compile_bot(g)
    assert compiled.schedules[0].value == "interval 1"

    async def scenario():
        store = BotStore(tmp_path / "b.json")
        tr = EmulatorTransport()
        eng = BotEngine("p", compiled, tr, store)
        await eng.handle({"chat": "7", "user": {"id": 7, "name": "Ира", "username": ""}, "text": "/later"})
        await asyncio.sleep(0.1)
        assert [m["text"] for m in tr.messages] == ["Напомню"]
        due = store.pending()[0]["due"]
        assert 7190 < due - time.time() <= 7200
        # перезапуск сервера: новое хранилище с того же файла — задача на месте и срабатывает в срок
        store2 = BotStore(tmp_path / "b.json")
        eng2 = BotEngine("p", compiled, tr, store2)
        await eng2.tick(due - 10)
        await asyncio.sleep(0.05)
        assert [m["text"] for m in tr.messages] == ["Напомню"]
        await eng2.tick(due + 1)
        await asyncio.sleep(0.1)
        assert tr.messages[-1]["text"] == "Пора, Ира!" and not store2.pending()
        # расписание «каждую минуту»: первый тик запоминает время, через минуту — рассылка всем пользователям
        eng2 = BotEngine("p", compiled, tr, store2)
        t0 = time.time()
        await eng2.tick(t0)
        await asyncio.sleep(0.05)
        n = len(tr.messages)
        await eng2.tick(t0 + 61)
        await asyncio.sleep(0.1)
        assert tr.messages[-1]["text"].startswith("Отчёт ") and tr.messages[-1]["chat"] == "7" and len(tr.messages) == n + 1
        await eng2.tick(t0 + 62)
        await asyncio.sleep(0.05)
        assert len(tr.messages) == n + 1      # не чаще раза в интервал

    asyncio.run(scenario())


def test_download_stats_export(platform, monkeypatch):
    import base64
    import io as _io

    from app import telegram as tg
    fake = FakeTelegram()
    orig = fake.handler
    csv_bytes = "имя;сумма\nАня;10\n".encode()

    async def handler(request):
        if "/file/bot" in request.url.path:
            fake.calls.append(("FILE", request.url.path))
            return httpx.Response(200, content=csv_bytes)
        method = request.url.path.rsplit("/", 1)[-1]
        if method == "getFile":
            fake.calls.append((method, json.loads(request.content)))
            return httpx.Response(200, json={"ok": True, "result": {"file_path": "documents/f.csv"}})
        return await orig(request)

    monkeypatch.setattr(tg, "TRANSPORT", httpx.MockTransport(handler))
    c = platform
    pid = c.post("/api/projects", json={"name": "Файлы", "kind": "bot"}).json()["id"]
    g = {"name": "Файлы", "kind": "bot", "env": [{"key": "BOT_TOKEN", "value": "7:x", "secret": True}],
         "bot": {"token_env": "BOT_TOKEN", "roles": ["moderator"], "admins": []},
         "nodes": [node("start", "bot_command", command="start"),
                   node("hi", "bot_send", text="Пришлите CSV", links=[], to="current"),
                   node("f", "bot_file", kind="document", where="any", roles=[]),
                   node("dl", "bot_download", source="file", mode="text", var="doc", max_mb="5"),
                   node("sv", "bot_save", assignments=[{"target": "last_file", "expr": "doc['name']"}]),
                   node("ok", "bot_send", text="{doc['name']}: {len(doc['text'])} символов, строк {len(doc['text']) - len(strip(doc['text'])) + 1}", links=[], to="current"),
                   node("err", "bot_send", text="Ошибка: {doc['error']}", links=[], to="current")],
         "edges": [edge("start", "hi"), edge("f", "dl"), edge("dl", "sv", "ok"), edge("sv", "ok"), edge("dl", "err", "error")]}
    assert c.put(f"/api/projects/{pid}", json=g).status_code == 200
    assert c.post(f"/api/projects/{pid}/bot/start").status_code == 200
    fake.push({"message": {"message_id": 1, "chat": {"id": 7, "type": "private"}, "from": {"id": 7, "first_name": "Ира"},
                           "text": "/start"}})
    fake.push({"message": {"message_id": 2, "chat": {"id": 7, "type": "private"}, "from": {"id": 7, "first_name": "Ира"},
                           "document": {"file_id": "DOC1", "file_name": "отчёт.csv", "mime_type": "text/csv",
                                        "file_size": len(csv_bytes)}}})
    _wait(lambda: fake.texts(7) and fake.texts(7)[-1].startswith("отчёт.csv:"))
    assert ("getFile", {"file_id": "DOC1"}) in fake.calls and any(m == "FILE" and p.endswith("documents/f.csv") for m, p in fake.calls)
    files = c.get(f"/api/projects/{pid}/bot/files").json()
    assert files[0]["name"] == "отчёт.csv" and files[0]["size"] == len(csv_bytes) and files[0]["user_name"] == "Ира"
    r = c.get(f"/api/projects/{pid}/bot/files/{files[0]['id']}")
    assert r.status_code == 200 and r.content == csv_bytes

    # статистика живого бота
    _wait(lambda: c.get(f"/api/projects/{pid}/bot/stats").json()["users_total"] == 1)
    st = c.get(f"/api/projects/{pid}/bot/stats").json()
    assert st["active_today"] == 1 and st["messages_today"] == 2 and ["/start", 1] in st["commands"]
    assert st["nodes"]["dl"] == 1 and st["days"][-1]["users"] == 1 and len(st["days"]) == 14

    # выгрузка: saved-ключи — колонки
    r = c.get(f"/api/projects/{pid}/bot/export?what=users&format=csv")
    text = r.content.decode("utf-8-sig")
    assert r.headers["content-disposition"].endswith('.csv"') and "last_file" in text.splitlines()[0]
    assert "отчёт.csv" in text and "Ира" in text
    from openpyxl import load_workbook
    wb = load_workbook(_io.BytesIO(c.get(f"/api/projects/{pid}/bot/export?what=users&format=xlsx").content))
    rows = list(wb.active.values)
    assert rows[0][-1] == "last_file" and rows[1][1] == "Ира" and rows[1][-1] == "отчёт.csv"
    assert c.get(f"/api/projects/{pid}/bot/export?what=payments&format=xlsx").status_code == 200
    c.post(f"/api/projects/{pid}/bot/stop")

    # base64 в тест-чате: файл из 📎 доходит до Python-кода
    g2 = {**g, "nodes": [n for n in g["nodes"]], "edges": list(g["edges"])}
    g2["nodes"][3] = node("dl", "bot_download", source="file", mode="base64", var="doc", max_mb="5")
    g2["nodes"][5] = node("ok", "bot_send", text="{doc['name']} {doc['size']} байт", links=[], to="current")
    data_url = "data:text/csv;base64," + base64.b64encode(csv_bytes).decode()
    st = c.post(f"/api/projects/{pid}/bot/emulator", json={"graph": g2, "reset": True,
                                                            "file": {"name": "a.csv", "mime": "text/csv", "size": len(csv_bytes), "data": data_url}}).json()
    assert [m["text"] for m in st["messages"] if m["from"] == "bot"][-1] == f"a.csv {len(csv_bytes)} байт"
