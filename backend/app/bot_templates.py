"""Готовые шаблоны ботов: проект создаётся с этим графом, дальше его правят под себя.

Каждый шаблон проверяется тестом: компилируется без ошибок и проходит основной сценарий в тест-чате.
"""

from __future__ import annotations


class _G:
    """Мини-конструктор графа: блоки, связи и раскладка «ветка — строка, развилки — ниже»."""

    def __init__(self):
        self.nodes: list[dict] = []
        self.edges: list[dict] = []

    def n(self, nid, type_, **data):
        self.nodes.append({"id": nid, "type": type_, "position": {"x": 0, "y": 0}, "data": data})
        return nid

    def e(self, s, t, handle=None):
        edge = {"id": f"e-{s}-{handle or 'o'}-{t}", "source": s, "target": t}
        if handle:
            edge["sourceHandle"] = handle
        self.edges.append(edge)

    def chain(self, *ids):
        for a, b in zip(ids, ids[1:]):
            self.e(a, b)

    def layout(self, starts: list[str]):
        out: dict[str, list[tuple[str | None, str]]] = {}
        for e in self.edges:
            out.setdefault(e["source"], []).append((e.get("sourceHandle"), e["target"]))
        pos: dict[str, tuple[int, int]] = {}
        row = 0

        def place(nid, depth, r):
            if nid in pos:
                return 0
            pos[nid] = (depth, r)
            children = out.get(nid, [])
            used = 0
            for _, child in children:
                used += place(child, depth + 1, r + used) or 0
            return max(used, 1)

        for s in starts:
            row += place(s, 0, row) + 0
        for nid in [n["id"] for n in self.nodes if n["id"] not in pos]:
            pos[nid] = (0, row)
            row += 1
        for n in self.nodes:
            col, r = pos[n["id"]]
            n["position"] = {"x": 40 + col * 300, "y": 40 + r * 170}
        return self


def _send(text, to="current", **kw):
    return {"text": text, "links": [], "to": to, "chat": "", "role": kw.pop("role", "admin"), "count_var": "", **kw}


def booking() -> dict:
    g = _G()
    s = g.n("start", "bot_command", command="start", description="Записаться", where="private", roles=[])
    hi = g.n("hello", "bot_send", **_send("Здравствуйте, {user_name}! Я помогу записаться на приём."))
    menu = g.n("service", "bot_menu", text="Выберите услугу:", var="service", timeout="3600", remove=True,
               keyboard="inline", columns="1",
               buttons=[{"id": "consult", "text": "Консультация"}, {"id": "haircut", "text": "Стрижка"},
                        {"id": "massage", "text": "Массаж"}])
    name = g.n("ask_name", "bot_ask", text="Как к вам обращаться?", var="name", type="text", timeout="3600",
               retry="Напишите имя текстом", button="📱 Поделиться номером")
    date = g.n("ask_date", "bot_ask", text="Когда вам удобно? Например: «пятница, 15:00»", var="date", type="text",
               timeout="3600", retry="Напишите дату и время текстом", button="📱 Поделиться номером")
    phone = g.n("ask_phone", "bot_ask", text="Оставьте номер телефона — нажмите кнопку ниже", var="phone",
                type="contact", timeout="3600", retry="Нажмите кнопку «Поделиться номером»",
                button="📱 Поделиться номером")
    save = g.n("save", "bot_save", assignments=[{"target": "name", "expr": "name"}, {"target": "service", "expr": "service"},
                                                {"target": "date", "expr": "date"}, {"target": "phone", "expr": "phone"}])
    ok = g.n("confirm", "bot_send", **_send("Готово, {name}! Вы записаны: {service}, {date}.\nНапомним накануне. "
                                            "Посмотреть запись — /my, отменить — /cancel"))
    admin = g.n("to_admin", "bot_send", **_send("📅 Новая запись: {service}, {date}\n{name}, {phone}", to="role"))
    delay = g.n("remind", "bot_delay", amount="1", unit="days")
    rem = g.n("reminder", "bot_send", **_send("Напоминаем о записи: {saved['service']}, {saved['date']}. "
                                              "Если планы изменились — /cancel"))
    late = g.n("late", "bot_send", **_send("Если захотите записаться — нажмите /start"))
    g.e(s, hi)
    g.e(hi, menu)
    for b in ("consult", "haircut", "massage"):
        g.e(menu, name, b)
    g.e(menu, late, "timeout")
    g.e(name, date, "ok")
    g.e(date, phone, "ok")
    g.e(phone, save, "ok")
    g.chain(save, ok, admin, delay)
    g.e(delay, rem, "later")
    my = g.n("my", "bot_command", command="my", description="Моя запись", where="private", roles=[])
    has = g.n("has", "condition", expr="'service' in saved and saved['service'] != ''")
    show = g.n("show", "bot_send", **_send("Ваша запись: {saved['service']}, {saved['date']}"))
    none = g.n("none", "bot_send", **_send("Записей нет. Записаться — /start"))
    g.e(my, has)
    g.e(has, show, "true")
    g.e(has, none, "false")
    cancel = g.n("cancel", "bot_command", command="cancel", description="Отменить запись", where="private", roles=[])
    clear = g.n("clear", "bot_save", assignments=[{"target": "service", "expr": "''"}])
    done = g.n("cancelled", "bot_send", **_send("Запись отменена."))
    g.chain(cancel, clear, done)
    g.layout([s, my, cancel])
    return {"nodes": g.nodes, "edges": g.edges, "bot": {"roles": ["moderator"], "admins": []}}


def shop() -> dict:
    g = _G()
    s = g.n("start", "bot_command", command="start", description="Каталог", where="private", roles=[])
    menu = g.n("catalog", "bot_menu", text="Привет, {user_name}! Что хотите купить?", var="item", timeout="3600",
               remove=True, keyboard="inline", columns="1",
               buttons=[{"id": "tshirt", "text": "👕 Футболка — 50 ⭐"}, {"id": "mug", "text": "☕ Кружка — 30 ⭐"},
                        {"id": "stickers", "text": "✨ Стикеры — 10 ⭐"}])
    items = {"tshirt": ("Футболка", 50), "mug": ("Кружка", 30), "stickers": ("Стикеры", 10)}
    inv = g.n("invoice", "bot_invoice", title="{title}", description="Оплата заказа «{title}»", amount="price",
              currency="XTR", timeout="900", var="payment")
    g.e(s, menu)
    for key, (title, price) in items.items():
        v = g.n(f"set_{key}", "set_vars", assignments=[{"target": "title", "expr": repr(title)},
                                                       {"target": "price", "expr": str(price)}])
        g.e(menu, v, key)
        g.e(v, inv)
    count = g.n("count", "bot_save", assignments=[
        {"target": "orders", "expr": "saved['orders'] + 1 if 'orders' in saved else 1"},
        {"target": "last_order", "expr": "title"}])
    thanks = g.n("thanks", "bot_send", **_send("Спасибо! Заказ «{title}» оплачен ({payment['amount']} ⭐). "
                                               "Мы свяжемся с вами. Ваши покупки — /orders"))
    admin = g.n("to_admin", "bot_send", **_send("🛒 Заказ: {title} — {payment['amount']} ⭐ от {user_name} "
                                                "(id {user_id}), платёж {payment['charge_id']}", to="role"))
    unpaid = g.n("unpaid", "bot_send", **_send("Оплата не прошла. Попробовать ещё раз — /start"))
    g.e(inv, count, "paid")
    g.chain(count, thanks, admin)
    g.e(inv, unpaid, "timeout")
    orders = g.n("orders", "bot_command", command="orders", description="Мои покупки", where="private", roles=[])
    o_send = g.n("orders_send", "bot_send", **_send(
        "Покупок: {saved['orders'] if 'orders' in saved else 0}"
        "{'. Последняя: ' + saved['last_order'] if 'last_order' in saved else ''}"))
    g.chain(orders, o_send)
    g.layout([s, orders])
    return {"nodes": g.nodes, "edges": g.edges, "bot": {"roles": ["moderator"], "admins": []}}


def survey() -> dict:
    g = _G()
    s = g.n("start", "bot_command", command="start", description="Пройти анкету", where="private", roles=[])
    hi = g.n("intro", "bot_send", **_send("Здравствуйте! Ответьте на 3 коротких вопроса — это займёт минуту."))
    q1 = g.n("q_name", "bot_ask", text="Как вас зовут?", var="name", type="text", timeout="1800",
             retry="Напишите имя", button="📱 Поделиться номером")
    q2 = g.n("q_age", "bot_ask", text="Сколько вам лет?", var="age", type="int", timeout="1800",
             retry="Напишите возраст числом, например 30", button="📱 Поделиться номером")
    q3 = g.n("q_source", "bot_menu", text="Откуда вы о нас узнали?", var="source", timeout="1800", remove=True,
             keyboard="reply", columns="2",
             buttons=[{"id": "friends", "text": "От друзей"}, {"id": "search", "text": "Поиск"},
                      {"id": "social", "text": "Соцсети"}, {"id": "other", "text": "Другое"}])
    save = g.n("save", "bot_save", assignments=[{"target": "name", "expr": "name"}, {"target": "age", "expr": "age"},
                                                {"target": "source", "expr": "source"},
                                                {"target": "completed", "expr": "True"}])
    thanks = g.n("thanks", "bot_send", **_send("Спасибо, {name}! Анкета сохранена."))
    admin = g.n("to_admin", "bot_send", **_send("📝 Новая анкета: {name}, {age} лет, источник: {source}", to="role"))
    late = g.n("late", "bot_send", **_send("Анкета не завершена. Начать заново — /start"))
    g.chain(s, hi, q1)
    g.e(q1, q2, "ok")
    g.e(q2, q3, "ok")
    for b in ("friends", "search", "social", "other"):
        g.e(q3, save, b)
    for q in (q1, q2):
        g.e(q, late, "timeout")
    g.e(q3, late, "timeout")
    g.chain(save, thanks, admin)
    remind = g.n("remind", "bot_schedule", kind="daily", at="19:00", weekdays=["fri"], every_minutes="60")
    rem_send = g.n("remind_send", "bot_send", **_send("Новых анкет за неделю — смотрите вкладку «Пользователи»; "
                                                      "выгрузка — «Статистика → Excel»", to="role"))
    g.chain(remind, rem_send)
    g.layout([s, remind])
    return {"nodes": g.nodes, "edges": g.edges, "bot": {"roles": ["moderator"], "admins": []}}


def moderator() -> dict:
    g = _G()
    join = g.n("join", "bot_member", action="join")
    welcome = g.n("welcome", "bot_send", **_send("Добро пожаловать, {user_name}! Правила чата — /rules"))
    g.chain(join, welcome)
    anym = g.n("any", "bot_message", match="any", pattern="", ignore_case=True, where="group", roles=[])
    spam = g.n("spam", "bot_filter", links=True, words=["казино", "заработок без вложений", "ставки"], caps=True,
               max_length="0", flood_count="5", flood_seconds="10", skip_admins=True, var="reason")
    delete = g.n("delete", "bot_moderate", action="delete", target="author", target_id="", minutes="0", var="err")
    warn = g.n("warn", "bot_send", **_send("{user_name}, сообщение удалено: {reason}"))
    g.e(anym, spam)
    g.e(spam, delete, "violation")
    g.e(delete, warn, "ok")
    rules = g.n("rules", "bot_command", command="rules", description="Правила чата", where="any", roles=[])
    rules_send = g.n("rules_send", "bot_send", **_send("Правила: без рекламы и ссылок, без капса и флуда, уважайте "
                                                       "друг друга. Нарушения удаляются автоматически."))
    g.chain(rules, rules_send)
    for cmd, action, minutes, text in (("mute", "mute", "60", "{reply_user_name} помолчит час"),
                                       ("ban", "ban", "0", "{reply_user_name} забанен"),
                                       ("unmute", "unmute", "0", "{reply_user_name} снова может писать")):
        c = g.n(cmd, "bot_command", command=cmd, description={"mute": "Замьютить (ответом)", "ban": "Забанить (ответом)",
                                                               "unmute": "Снять мьют (ответом)"}[cmd],
                where="group", roles=[])
        chk = g.n(f"{cmd}_chk", "condition", expr="is_chat_admin or is_admin")
        act = g.n(f"{cmd}_do", "bot_moderate", action=action, target="reply", target_id="", minutes=minutes, var="err")
        ok = g.n(f"{cmd}_ok", "bot_send", **_send(text))
        bad = g.n(f"{cmd}_err", "bot_send", **_send("Не получилось: {err}"))
        deny = g.n(f"{cmd}_deny", "bot_send", **_send("Команда только для администраторов чата"))
        g.e(c, chk)
        g.e(chk, act, "true")
        g.e(chk, deny, "false")
        g.e(act, ok, "ok")
        g.e(act, bad, "error")
    g.layout([join, anym, rules, "mute", "ban", "unmute"])
    return {"nodes": g.nodes, "edges": g.edges, "bot": {"roles": ["moderator"], "admins": []}}


TEMPLATES = {
    "booking": ("Запись на приём", "Выбор услуги, имя, дата, телефон кнопкой, подтверждение, уведомление админу, "
                                   "напоминание через день; /my и /cancel", booking),
    "shop": ("Магазин со Stars", "Каталог кнопками, счёт в Telegram Stars, уведомление админу о заказе, /orders",
             shop),
    "survey": ("Анкета", "Вопросы с проверкой числа, выбор на клавиатуре, сохранение ответов (выгрузка в Excel), "
                         "уведомление админу, еженедельное напоминание по расписанию", survey),
    "moderator": ("Модератор группы", "Приветствие новичков, антиспам (ссылки, стоп-слова, капс, флуд), "
                                      "/mute /ban /unmute ответом — для админов чата, /rules", moderator),
}


def listing() -> list[dict]:
    return [{"id": k, "title": t, "description": d} for k, (t, d, _) in TEMPLATES.items()]


def build(template_id: str) -> dict | None:
    item = TEMPLATES.get(template_id)
    return item[2]() if item else None
