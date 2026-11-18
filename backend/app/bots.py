"""Конструктор Telegram-ботов: третий тип проектов.

Граф — те же React Flow-блоки. Каждый блок-событие («Команда», «Сообщение») начинает ветку-обработчик;
компиляция превращает её в план, а движок (BotEngine) выполняет план на каждое входящее сообщение.
Блоки «Спросить» и «Меню» ждут ответа пользователя: обработчик — корутина, которая засыпает до его
следующего сообщения или нажатия кнопки (состояние диалога живёт в памяти, перезапуск сервера его сбрасывает).

Транспорты: TelegramTransport (настоящий бот, long polling) и EmulatorTransport (тест-чат в редакторе).
Данные пользователей бота («Запомнить») — data/bots/<проект>.json.
"""

from __future__ import annotations

import asyncio
import json
import os
import re
import threading
import time
import uuid
from collections import deque
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from dragapi_agent.runner import jsonable, render_template

from . import expr, sandbox, storage, vault
from .automation import (
    F, TelegramSettings, _check_expr, _check_ident, _compile_env, _Walker,
)
from .firewall import FirewallSettings
from .graph import EnvVar, GraphError, Issue
from .paths import DATA
from .pyenv import validate_packages
from .runtime import apply_code_result

BOTS_DIR = DATA / "bots"
COMMAND_RE = re.compile(r"^[a-z0-9_]{1,32}$")
MAX_STEPS = 2000
WAIT_LIMIT = 7 * 86400

ROLE_RE = re.compile(r"^[a-z][a-z0-9_]{0,31}$")
BUILTIN_ROLES = ["admin", "user", "banned"]
CURRENCIES = [["XTR", "⭐ Telegram Stars"], ["RUB", "RUB — рубли"], ["USD", "USD — доллары"], ["EUR", "EUR — евро"]]
ROLES_FIELD = dict(key="roles", label="Доступна ролям", kind="roles", optional=True,
                   hint="Пусто — всем. Администраторы проходят всегда. Остальным бот ответит текстом из настроек.")

ROUTE_OPTS = [["any", "Любое сообщение"], ["equals", "Текст равен"], ["contains", "Текст содержит"],
              ["regex", "Регулярное выражение"]]

BOT_BLOCKS: dict[str, dict] = {
    "bot_command": {
        "title": "Команда", "group": "События", "color": "#229ED9", "icon": "/",
        "description": "Начало ветки: пользователь отправил команду, например /start. Текст после команды — в args.",
        "inputs": [], "output": ["flow"],
        "defaults": {"command": "start", "description": "Начать"},
        "fields": [F(key="command", label="Команда (без /)", kind="text",
                     hint="Латиница в нижнем регистре, цифры и _. Появится в меню бота."),
                   F(key="description", label="Описание в меню", kind="text", optional=True),
                   F(**ROLES_FIELD)],
    },
    "bot_message": {
        "title": "Сообщение", "group": "События", "color": "#229ED9", "icon": "✉",
        "description": "Начало ветки: пользователь написал текст. Точное совпадение важнее «содержит», «любое» — последнее.",
        "inputs": [], "output": ["flow"],
        "defaults": {"match": "contains", "pattern": "привет", "ignore_case": True},
        "fields": [F(key="match", label="Условие", kind="select", options=ROUTE_OPTS),
                   F(key="pattern", label="Текст или выражение", kind="text",
                     show_if={"match": ["equals", "contains", "regex"]}),
                   F(key="ignore_case", label="Без учёта регистра", kind="bool",
                     show_if={"match": ["equals", "contains", "regex"]}),
                   F(**ROLES_FIELD)],
    },
    "bot_send": {
        "title": "Отправить сообщение", "group": "Сообщения", "color": "#0ea5e9", "icon": "💬",
        "description": "Текст с {переменными} и кнопками-ссылками — в этот чат, в другой или рассылкой по роли.",
        "inputs": ["flow"], "output": ["flow"],
        "defaults": {"text": "Привет, {user_name}!", "links": [], "to": "current", "chat": "", "role": "admin",
                     "count_var": ""},
        "fields": [F(key="text", label="Текст", kind="template", multiline=True),
                   F(key="links", label="Кнопки-ссылки", kind="links"),
                   F(key="to", label="Кому", kind="select", options=[
                       ["current", "В этот чат"], ["chat", "В чат по ID"], ["role", "Всем с ролью (рассылка)"]]),
                   F(key="chat", label="ID чата (или {выражение})", kind="template", show_if={"to": ["chat"]},
                     hint="Например, ID администратора: 123456789"),
                   F(key="role", label="Роль получателей", kind="role_any", show_if={"to": ["role"]},
                     hint="«все пользователи» — рассылка всем, кто писал боту (кроме заблокированных)"),
                   F(key="count_var", label="Число отправленных — в переменную", kind="text", optional=True,
                     show_if={"to": ["role"]})],
    },
    "bot_photo": {
        "title": "Отправить фото", "group": "Сообщения", "color": "#0ea5e9", "icon": "🖼",
        "description": "Картинка по ссылке (https://…) с подписью.",
        "inputs": ["flow"], "output": ["flow"],
        "defaults": {"url": "https://picsum.photos/600/400", "caption": ""},
        "fields": [F(key="url", label="Адрес картинки", kind="template"),
                   F(key="caption", label="Подпись", kind="template", optional=True)],
    },
    "bot_ask": {
        "title": "Спросить", "group": "Сообщения", "color": "#f59e0b", "icon": "❔",
        "description": "Задать вопрос и дождаться ответа текстом — он попадёт в переменную.",
        "inputs": ["flow"], "output": ["flow"],
        "branches": [{"id": "ok", "label": "ответ"}, {"id": "timeout", "label": "нет ответа"}],
        "defaults": {"text": "Как вас зовут?", "var": "answer", "type": "text",
                     "retry": "Нужно число — попробуйте ещё раз", "timeout": "3600"},
        "fields": [F(key="text", label="Вопрос", kind="template", multiline=True),
                   F(key="var", label="Ответ — в переменную", kind="text"),
                   F(key="type", label="Ожидаем", kind="select",
                     options=[["text", "Любой текст"], ["number", "Число"], ["int", "Целое число"]]),
                   F(key="retry", label="Если ответ не подходит", kind="template",
                     show_if={"type": ["number", "int"]}),
                   F(key="timeout", label="Ждать, с", kind="num", hint="Нет ответа — выход «нет ответа»")],
    },
    "bot_menu": {
        "title": "Меню с кнопками", "group": "Сообщения", "color": "#f59e0b", "icon": "☰",
        "description": "Сообщение с кнопками: у блока столько выходов, сколько кнопок. Ждёт нажатия.",
        "inputs": ["flow"], "output": ["flow"],
        "dynamic_branches": {"field": "buttons", "extra": [{"id": "timeout", "label": "нет ответа"}]},
        "defaults": {"text": "Что вас интересует?", "buttons": [{"id": "b1", "text": "Цены"},
                                                               {"id": "b2", "text": "Контакты"}],
                     "var": "choice", "timeout": "3600", "remove": True},
        "fields": [F(key="text", label="Текст", kind="template", multiline=True),
                   F(key="buttons", label="Кнопки (каждая — свой выход)", kind="buttons"),
                   F(key="var", label="Нажатая кнопка (текст) — в переменную", kind="text", optional=True),
                   F(key="remove", label="Убрать кнопки после нажатия", kind="bool"),
                   F(key="timeout", label="Ждать, с", kind="num")],
    },
    "bot_role": {
        "title": "Назначить роль", "group": "Админка", "color": "#9333ea", "icon": "🛡",
        "description": "Выдать пользователю роль (moderator, banned…) по ID или @username. «user» — снять роль.",
        "inputs": ["flow"], "output": ["flow"],
        "branches": [{"id": "ok", "label": "готово"}, {"id": "not_found", "label": "не найден"}],
        "defaults": {"target": "{args}", "role": "moderator", "var": "target_user"},
        "fields": [F(key="target", label="Кому (ID или @username)", kind="template", optional=True,
                     hint="Пусто — текущему пользователю. {args} — то, что написано после команды: /mod @ivan"),
                   F(key="role", label="Роль", kind="role"),
                   F(key="var", label="Пользователь — в переменную", kind="text", optional=True,
                     hint="{id, name, username, role} — например, чтобы сообщить ему")],
    },
    "bot_invoice": {
        "title": "Счёт на оплату", "group": "Оплата", "color": "#16a34a", "icon": "💳",
        "description": "Выставить счёт и дождаться оплаты: выходы «оплачено» и «не оплачено».",
        "inputs": ["flow"], "output": ["flow"],
        "branches": [{"id": "paid", "label": "оплачено"}, {"id": "timeout", "label": "не оплачено"}],
        "defaults": {"title": "Подписка", "description": "Доступ к материалам на месяц", "amount": "50",
                     "currency": "XTR", "timeout": "900", "var": "payment"},
        "fields": [F(key="title", label="Название (до 32 символов)", kind="template"),
                   F(key="description", label="Описание", kind="template", multiline=True),
                   F(key="currency", label="Валюта", kind="select", options=CURRENCIES,
                     hint="Stars работают без договора с банком; рубли и др. — через провайдера (настройки → Бот)"),
                   F(key="amount", label="Сумма", kind="num", hint="В звёздах или в рублях/долларах (10.50)"),
                   F(key="timeout", label="Ждать оплату, с", kind="num"),
                   F(key="var", label="Платёж — в переменную", kind="text", optional=True,
                     hint="{amount, currency, charge_id}")],
    },
    "condition": {
        "title": "Условие", "group": "Логика", "color": "#ec4899", "icon": "◇",
        "description": "Ветвление: «да», если выражение истинно, иначе «нет».",
        "inputs": ["flow"], "output": ["flow"],
        "branches": [{"id": "true", "label": "да"}, {"id": "false", "label": "нет"}],
        "defaults": {"expr": "contains(text, '?')"},
        "fields": [F(key="expr", label="Выражение", kind="expr")],
    },
    "set_vars": {
        "title": "Переменные", "group": "Логика", "color": "#10b981", "icon": "=",
        "description": "Задать переменные на время этого диалога.",
        "inputs": ["flow"], "output": ["flow"],
        "defaults": {"assignments": [{"target": "greeting", "expr": "'Здравствуйте, ' + user_name"}]},
        "fields": [F(key="assignments", label="Присваивания", kind="assignments")],
    },
    "bot_save": {
        "title": "Запомнить", "group": "Логика", "color": "#10b981", "icon": "💾",
        "description": "Сохранить данные пользователя навсегда; читаются как saved['ключ'].",
        "inputs": ["flow"], "output": ["flow"],
        "defaults": {"assignments": [{"target": "name", "expr": "answer"}]},
        "fields": [F(key="assignments", label="Ключ = выражение", kind="assignments")],
    },
    "wait": {
        "title": "Пауза", "group": "Логика", "color": "#64748b", "icon": "⏱",
        "description": "Подождать N секунд (до 60) — например, «бот печатает…».",
        "inputs": ["flow"], "output": ["flow"],
        "defaults": {"seconds": "1"},
        "fields": [F(key="seconds", label="Секунд", kind="num")],
    },
    "stop": {
        "title": "Завершить", "group": "Логика", "color": "#ef4444", "icon": "■",
        "description": "Закончить обработку сообщения.",
        "inputs": ["flow"], "output": [],
        "defaults": {},
        "fields": [],
    },
    "http": {
        "title": "HTTP-запрос", "group": "Данные", "color": "#7c3aed", "icon": "🌐",
        "description": "Запрос к внешнему API (по правилам firewall проекта). Ответ — в переменную.",
        "inputs": ["flow"], "output": ["flow"],
        "defaults": {"method": "GET", "url": "https://api.github.com/zen", "body": [], "target": "response",
                     "timeout": "10"},
        "fields": [F(key="method", label="Метод", kind="select",
                     options=[[m, m] for m in ["GET", "POST", "PUT", "PATCH", "DELETE"]]),
                   F(key="url", label="Адрес", kind="template"),
                   F(key="body", label="JSON-тело", kind="kv", show_if={"method": ["POST", "PUT", "PATCH", "DELETE"]}),
                   F(key="target", label="Ответ — в переменную", kind="text"),
                   F(key="timeout", label="Таймаут, с (до 10)", kind="text", optional=True)],
    },
    "code": {
        "title": "Python-код", "group": "Данные", "color": "#0f766e", "icon": "🐍",
        "description": "run(data, env) в песочнице; data — переменные диалога.",
        "inputs": ["flow"], "output": ["flow"],
        "defaults": {
            "code": ("def run(data, env):\n"
                     "    # data — переменные диалога (text, user_name, saved…), env — переменные окружения.\n"
                     "    return {\"reply\": str(data.get(\"text\", \"\"))[::-1]}\n"),
            "outputs": ["reply"], "timeout": 2, "memory_mb": 256,
        },
        "fields": [F(key="code", label="Код", kind="code")],
    },
    "run_scenario": {
        "title": "Запустить сценарий", "group": "Данные", "color": "#6366f1", "icon": "🤖",
        "description": "Сценарий из «Автоматизации» (со «Стартом» «Вызов по HTTP», сценарии включены).",
        "inputs": ["flow"], "output": ["flow"],
        "defaults": {"project": "", "scenario": "main", "params": [], "wait": True, "target": "scenario"},
        "fields": [F(key="project", label="Проект-автоматизация", kind="automation_project"),
                   F(key="scenario", label="Сценарий", kind="text"),
                   F(key="params", label="Параметры", kind="kv"),
                   F(key="wait", label="Дождаться результата", kind="bool"),
                   F(key="target", label="Результат — в переменную", kind="text", optional=True,
                     hint="{status, message, vars}")],
    },
}

START_TYPES = ("bot_command", "bot_message")


def can_connect(source_type: str, target_type: str) -> bool:
    src, dst = BOT_BLOCKS.get(source_type), BOT_BLOCKS.get(target_type)
    return bool(src and dst and set(src["output"]) & set(dst["inputs"]))


def catalog() -> dict:
    return {"blocks": BOT_BLOCKS}


def branch_ids(ntype: str, data: dict) -> list[str] | None:
    spec = BOT_BLOCKS.get(ntype) or {}
    if "branches" in spec:
        return [b["id"] for b in spec["branches"]]
    dyn = spec.get("dynamic_branches")
    if dyn:
        return [str(b.get("id")) for b in data.get(dyn["field"]) or []] + [b["id"] for b in dyn["extra"]]
    return None


# ---------- компиляция ----------

@dataclass
class Route:
    node: str
    kind: str                # command | equals | contains | regex | any
    value: str = ""
    ignore_case: bool = True
    description: str = ""
    steps: list[dict] = field(default_factory=list)
    roles: list[str] = field(default_factory=list)

    def matches(self, text: str) -> bool:
        t, v = (text.lower(), self.value.lower()) if self.ignore_case else (text, self.value)
        if self.kind == "equals":
            return t.strip() == v.strip()
        if self.kind == "contains":
            return v in t
        if self.kind == "regex":
            return re.search(self.value, text, re.IGNORECASE if self.ignore_case else 0) is not None
        return self.kind == "any"


@dataclass
class CompiledBot:
    name: str
    commands: dict[str, Route]
    messages: list[Route]
    env: list[EnvVar]
    warnings: list[Issue]
    token_env: str
    sandbox: sandbox.SandboxSettings
    packages: list[str]
    firewall: FirewallSettings
    code: dict[str, dict] = field(default_factory=dict)
    http: dict[str, dict] = field(default_factory=dict)
    roles: list[str] = field(default_factory=lambda: ["moderator"])   # свои роли (кроме встроенных)
    admins: list[str] = field(default_factory=list)                    # ID администраторов из настроек
    deny_text: str = "Нет доступа"
    payments_env: str = ""

    @property
    def env_values(self) -> dict[str, str]:
        return {e.key: e.value for e in self.env}

    @property
    def token(self) -> str:
        return self.env_values.get(self.token_env, "") if self.token_env else ""

    @property
    def provider_token(self) -> str:
        return self.env_values.get(self.payments_env, "") if self.payments_env else ""

    def all_roles(self) -> list[str]:
        return ["admin", *self.roles, "user", "banned"]

    def route(self, text: str) -> tuple[Route | None, str]:
        """Ветка для текста: команда, затем «равен», «содержит»/регулярка, «любое». -> (ветка, args)."""
        text = text or ""
        if text.startswith("/"):
            head, _, rest = text[1:].partition(" ")
            cmd = head.split("@", 1)[0].lower()
            if cmd in self.commands:
                return self.commands[cmd], rest.strip()
        order = {"equals": 0, "contains": 1, "regex": 1, "any": 2}
        for r in sorted(self.messages, key=lambda r: order[r.kind]):
            if r.matches(text):
                return r, ""
        return None, ""

    def summary(self) -> list[dict]:
        out = [{"node": r.node, "kind": "command", "value": c, "description": r.description, "roles": r.roles}
               for c, r in self.commands.items()]
        return out + [{"node": r.node, "kind": r.kind, "value": r.value, "roles": r.roles} for r in self.messages]


class _BotWalker(_Walker):
    def __init__(self, nodes, outgoing, reached, err, warn, roles=(), payments=False):
        super().__init__(nodes, outgoing, reached, err, warn, TelegramSettings())
        self.blocks = BOT_BLOCKS
        self.roles, self.payments = list(roles), payments

    def walk(self, cur, path, loop=None):
        steps: list[dict] = []
        path = set(path)
        while cur is not None:
            if cur in path:
                self.fail(cur, "цикл в цепочке: ветки бота не могут возвращаться назад")
                return None
            self.budget -= 1
            if self.budget < 0:
                self.fail(cur, f"обработчик слишком разветвлён (больше {MAX_STEPS} шагов)")
                return None
            path.add(cur)
            self.reached.add(cur)
            node = self.nodes[cur]
            ntype, data = node.get("type"), node.get("data") or {}
            if ntype in START_TYPES:
                self.fail(cur, "событие может быть только началом ветки")
                return None
            step = self._step(cur, ntype, data)
            handles = branch_ids(ntype, data)
            if handles is not None:
                branches = {}
                for h in handles:
                    sub = self.walk(self.outgoing.get((cur, h)), frozenset(path))
                    if sub is None:
                        return None
                    branches[h] = sub
                if step is not None:
                    steps.append({**step, "branches": branches})
                return steps
            if step is not None:
                steps.append(step)
            if ntype == "stop":
                return steps
            cur = self.outgoing.get((cur, None))
        return steps

    def _step(self, nid, ntype, data):
        step = super()._step(nid, ntype, data)
        ok = step is not None
        spec = BOT_BLOCKS[ntype]

        def fail(msg):
            nonlocal ok
            ok = False
            self.fail(nid, msg)

        if ntype == "bot_menu":
            buttons = []
            for b in data.get("buttons") or []:
                text, bid = str(b.get("text") or "").strip(), str(b.get("id") or "").strip()
                if not text:
                    fail("у кнопки пустой текст")
                elif not re.fullmatch(r"[A-Za-z0-9_-]{1,20}", bid):
                    fail(f"кнопка «{text}»: некорректный id")
                else:
                    buttons.append({"id": bid, "text": text[:64]})
            if not buttons:
                fail("добавьте хотя бы одну кнопку")
            if len({b["id"] for b in buttons}) != len(buttons):
                fail("у кнопок повторяются id")
            if step is not None:
                step["buttons"] = buttons
        if ntype == "bot_send":
            links = []
            for b in data.get("links") or []:
                text, url = str(b.get("text") or "").strip(), str(b.get("url") or "").strip()
                if not text or not re.match(r"^(https?://|tg://)", url):
                    fail("кнопка-ссылка: нужен текст и адрес https://… или tg://…")
                else:
                    links.append({"text": text[:64], "url": url})
            if step is not None:
                step["links"] = links
        if ntype == "bot_ask" and step is not None and not step.get("var"):
            fail("укажите переменную для ответа")
        if ntype == "bot_send" and step is not None:
            step.setdefault("to", data.get("to") or ("chat" if data.get("chat") else "current"))
            if step["to"] == "chat" and not str(data.get("chat") or "").strip():
                fail("укажите ID чата")
            if step["to"] == "role":
                role = str(data.get("role") or "")
                if role not in ["all", "admin", *self.roles, "user"]:
                    fail(f"неизвестная роль «{role}» — добавьте её в «Настройки → Бот»")
                step["role"] = role
            if step.get("count_var") and (msg := _check_ident(step["count_var"], "переменная")):
                fail(msg)
        if ntype == "bot_role" and step is not None:
            role = str(data.get("role") or "")
            if role not in [*self.roles, "user", "banned"]:
                fail("роль: одна из ролей проекта, user (снять) или banned. Администраторы задаются в настройках")
            step["role"] = role
        if ntype == "bot_invoice" and step is not None:
            if step.get("currency") != "XTR" and not self.payments:
                fail("оплата в валюте требует токен платёжного провайдера (настройки → Бот); Stars работают без него")
        if ntype == "run_scenario" and step is not None:
            pid = str(data.get("project") or "")
            if not re.fullmatch(r"[a-z0-9][a-z0-9-]{0,63}", pid):
                fail("выберите проект-автоматизацию")
            if not re.fullmatch(r"[A-Za-z0-9_-]{1,40}", step.get("scenario") or ""):
                fail("имя сценария: латиница, цифры, - и _")
            step["project"] = pid
        _ = spec
        return step if ok else None


def compile_bot(graph: dict) -> CompiledBot:
    issues: list[Issue] = []

    def err(node_id, msg):
        issues.append(Issue(node_id, msg))

    def warn(node_id, msg):
        issues.append(Issue(node_id, msg, "warning"))

    env = _compile_env(graph.get("env"), err)
    settings = graph.get("bot") if isinstance(graph.get("bot"), dict) else {}
    token_env = str(settings.get("token_env") or "").strip()
    if token_env and token_env not in {e.key for e in env}:
        err(None, f"бот: в переменных проекта нет {token_env} — добавьте её (секрет) с токеном")
    roles = []
    for r in settings.get("roles", ["moderator"]) or []:
        r = str(r).strip().lower()
        if not ROLE_RE.match(r):
            err(None, f"роль «{r}»: латиница в нижнем регистре, цифры и _")
        elif r in BUILTIN_ROLES:
            err(None, f"роль «{r}» встроенная — её не нужно добавлять")
        elif r not in roles:
            roles.append(r)
    admins = []
    for a in settings.get("admins") or []:
        a = str(a).strip()
        if not re.fullmatch(r"\d{1,20}", a):
            err(None, f"ID администратора «{a}» — это число (узнать свой ID: напишите боту, он появится в «Пользователях»)")
        elif a not in admins:
            admins.append(a)
    payments_env = str(settings.get("payments_env") or "").strip()
    if payments_env and payments_env not in {e.key for e in env}:
        err(None, f"оплата: в переменных проекта нет {payments_env}")
    sandbox_settings, sandbox_errors = sandbox.SandboxSettings.parse(graph.get("sandbox"))
    libraries = graph.get("libraries") or {}
    packages, package_errors = validate_packages(libraries.get("packages"))
    firewall, firewall_errors = FirewallSettings.parse(graph.get("firewall"), legacy_network=sandbox_settings.network)
    for m in sandbox_errors + package_errors + firewall_errors:
        err(None, m)

    nodes = {n["id"]: n for n in graph.get("nodes", []) if isinstance(n, dict) and "id" in n}
    for nid, n in nodes.items():
        if n.get("type") not in BOT_BLOCKS:
            err(nid, f"неизвестный тип блока «{n.get('type')}»")
    outgoing: dict[tuple[str, str | None], str] = {}
    for e in graph.get("edges", []):
        s, t = e.get("source"), e.get("target")
        if s not in nodes or t not in nodes:
            err(None, f"связь {e.get('id')} ссылается на несуществующий блок")
            continue
        st, tt = nodes[s].get("type"), nodes[t].get("type")
        if not can_connect(st, tt):
            err(t, f"нельзя соединить «{BOT_BLOCKS.get(st, {}).get('title', st)}» → "
                   f"«{BOT_BLOCKS.get(tt, {}).get('title', tt)}»")
            continue
        handles = branch_ids(st, nodes[s].get("data") or {})
        handle = e.get("sourceHandle") if handles is not None else None
        if handles is not None and handle not in handles:
            err(s, f"связь из неизвестного выхода «{handle}» (кнопку удалили?)")
            continue
        if (s, handle) in outgoing:
            err(s, "из одного выхода может идти только одна связь")
            continue
        outgoing[(s, handle)] = t

    reached: set[str] = set()
    commands: dict[str, Route] = {}
    messages: list[Route] = []
    code, http = {}, {}
    for nid, n in nodes.items():
        ntype, data = n.get("type"), n.get("data") or {}
        if ntype not in START_TYPES:
            continue
        reached.add(nid)
        route = None
        route_roles = []
        for r in data.get("roles") or []:
            if r not in ["admin", *roles]:
                err(nid, f"неизвестная роль «{r}» — добавьте её в «Настройки → Бот»")
            else:
                route_roles.append(r)
        if ntype == "bot_command":
            cmd = str(data.get("command") or "").strip().lstrip("/").lower()
            if not COMMAND_RE.match(cmd):
                err(nid, "команда: латиница в нижнем регистре, цифры и _ (до 32 символов)")
            elif cmd in commands:
                err(nid, f"команда /{cmd} уже обрабатывается другой веткой")
            else:
                route = Route(nid, "command", cmd, description=str(data.get("description") or "")[:250])
                commands[cmd] = route
        else:
            match = data.get("match") or "any"
            pattern = str(data.get("pattern") or "")
            if match not in [o[0] for o in ROUTE_OPTS]:
                err(nid, f"неизвестное условие «{match}»")
            elif match != "any" and not pattern.strip():
                err(nid, "укажите текст")
            else:
                if match == "regex":
                    try:
                        re.compile(pattern)
                    except re.error as e:
                        err(nid, f"регулярное выражение: {e}")
                        continue
                route = Route(nid, match, pattern, bool(data.get("ignore_case", True)))
                messages.append(route)
        if route is not None:
            route.roles = route_roles
        walker = _BotWalker(nodes, outgoing, reached, err, warn, roles, bool(payments_env))
        steps = walker.walk(outgoing.get((nid, None)), frozenset({nid}))
        if route is not None and steps is not None and walker.ok:
            route.steps = steps
            if not steps:
                warn(nid, "к событию ничего не подключено — бот промолчит")
        code.update(walker.code)
        http.update(walker.http)
    if sum(r.kind == "any" for r in messages) > 1:
        err(None, "событие «Любое сообщение» может быть только одно")

    errors, seen = [], set()
    for i in issues:
        if i.level == "error" and (i.node_id, i.message) not in seen:
            seen.add((i.node_id, i.message))
            errors.append(i)
    if errors:
        raise GraphError(errors)
    warnings = [i for i in issues if i.level == "warning"]
    warnings += [Issue(nid, "блок не подключён ни к одному событию — он не будет выполняться", "warning")
                 for nid in nodes if nid not in reached]
    if not commands and not messages:
        warnings.append(Issue(None, "нет ни одного события — бот не будет отвечать", "warning"))
    elif "start" not in commands:
        warnings.append(Issue(None, "нет ветки /start — её Telegram отправляет первой, когда пользователь "
                                    "открывает бота", "warning"))
    if not token_env:
        warnings.append(Issue(None, "токен бота не задан (настройки → Бот) — работает только тест-чат", "warning"))
    if any(r.roles for r in [*commands.values(), *messages]) and not admins:
        warnings.append(Issue(None, "есть ветки только для ролей, но не задан ни один администратор "
                                    "(настройки → Бот)", "warning"))
    return CompiledBot(str(graph.get("name") or ""), commands, messages, env, warnings, token_env,
                       sandbox_settings, packages, firewall, code, http, roles, admins,
                       str(settings.get("deny_text") or "Нет доступа")[:500], payments_env)


# ---------- данные пользователей ----------

class BotStore:
    """Пользователи бота и их сохранённые данные. path=None — только в памяти (тест-чат)."""

    def __init__(self, path: Path | None):
        self.path = path
        self.lock = threading.Lock()
        self.data = {"users": {}, "payments": []}
        if path and path.exists():
            try:
                self.data = json.loads(path.read_text("utf-8"))
            except (OSError, ValueError):
                pass
        self.data.setdefault("payments", [])

    def _save(self):
        if self.path is None:
            return
        self.path.parent.mkdir(parents=True, exist_ok=True)
        tmp = self.path.with_suffix(".tmp")
        tmp.write_text(json.dumps(self.data, ensure_ascii=False, indent=1), "utf-8")
        tmp.replace(self.path)

    def touch(self, user: dict, chat: str):
        with self.lock:
            u = self.data["users"].setdefault(str(user["id"]), {"first_seen": time.time(), "saved": {}})
            u.update(name=user.get("name", ""), username=user.get("username", ""), chat=chat, last_seen=time.time())
            u["messages"] = u.get("messages", 0) + 1
            self._save()
            return dict(u.get("saved") or {})

    def save(self, user_id, values: dict):
        with self.lock:
            u = self.data["users"].setdefault(str(user_id), {"first_seen": time.time(), "saved": {}})
            u.setdefault("saved", {}).update(jsonable(values))
            self._save()
            return dict(u["saved"])

    def users(self) -> list[dict]:
        with self.lock:
            return sorted(({"id": k, **v} for k, v in self.data["users"].items()),
                          key=lambda u: -u.get("last_seen", 0))

    def role(self, user_id, admins: list[str]) -> str:
        if str(user_id) in admins:
            return "admin"
        with self.lock:
            return (self.data["users"].get(str(user_id)) or {}).get("role") or "user"

    def find(self, target: str) -> tuple[str, dict] | None:
        """ID или @username → (id, запись)."""
        target = str(target or "").strip()
        with self.lock:
            if target.lstrip("-").isdigit():
                u = self.data["users"].get(target)
                return (target, dict(u)) if u is not None else None
            name = target.lstrip("@").lower()
            for uid, u in self.data["users"].items():
                if name and (u.get("username") or "").lower() == name:
                    return uid, dict(u)
        return None

    def set_role(self, user_id, role: str):
        with self.lock:
            u = self.data["users"].setdefault(str(user_id), {"first_seen": time.time(), "saved": {}})
            if role == "user":
                u.pop("role", None)
            else:
                u["role"] = role
            self._save()
            return {"id": str(user_id), **u}

    def by_role(self, role: str, admins: list[str]) -> list[str]:
        """ID пользователей с ролью; all — все, кроме заблокированных."""
        with self.lock:
            items = list(self.data["users"].items())
        out = []
        for uid, u in items:
            r = "admin" if uid in admins else u.get("role") or "user"
            if r == "banned":
                continue
            if role == "all" or r == role:
                out.append(uid)
        if role in ("admin", "all"):
            out += [a for a in admins if a not in out]
        return out

    def add_payment(self, record: dict):
        with self.lock:
            self.data["payments"].append(record)
            self._save()

    def payments(self) -> list[dict]:
        with self.lock:
            return list(reversed(self.data["payments"]))

    def clear(self):
        with self.lock:
            self.data = {"users": {}, "payments": []}
            self._save()


# ---------- транспорты ----------

class EmulatorTransport:
    """Тест-чат в редакторе: сообщения бота складываются в ленту."""

    def __init__(self):
        self.messages: list[dict] = []
        self.seq = 0

    def _add(self, msg):
        self.seq += 1
        msg = {"id": self.seq, "t": time.time(), **msg}
        self.messages.append(msg)
        del self.messages[:-300]
        return self.seq

    def user_message(self, text):
        return self._add({"from": "user", "text": text})

    async def send(self, chat, text, buttons=None, links=None):
        return self._add({"from": "bot", "chat": str(chat), "text": text, "buttons": buttons or [],
                          "links": links or []})

    async def photo(self, chat, url, caption):
        return self._add({"from": "bot", "chat": str(chat), "photo": url, "text": caption})

    async def finish_menu(self, chat, message_id, note, remove):
        for m in self.messages:
            if m["id"] == message_id:
                if remove:
                    m["buttons"] = []
                m["note"] = note

    async def answer_callback(self, cq_id, text=""):
        pass

    async def invoice(self, chat, title, description, payload, currency, amount, provider_token):
        return self._add({"from": "bot", "chat": str(chat), "invoice": {
            "title": title, "description": description, "payload": payload, "currency": currency, "amount": amount},
            "text": f"💳 {title}: {_fmt_amount(amount, currency)}\n{description}"})

    async def answer_pre_checkout(self, qid, ok, error=None):
        pass

    async def set_commands(self, commands, chat_id=None):
        pass


class TelegramTransport:
    def __init__(self, token: str):
        self.token = token

    async def _call(self, method, **params):
        from .telegram import call
        return await call(self.token, method, **params)

    async def send(self, chat, text, buttons=None, links=None):
        rows = [[{"text": b["text"], "callback_data": b["id"]}] for b in buttons or []]
        rows += [[{"text": b["text"], "url": b["url"]}] for b in links or []]
        m = await self._call("sendMessage", chat_id=chat, text=text[:4096] or "…",
                             reply_markup={"inline_keyboard": rows} if rows else None)
        return m.get("message_id") if isinstance(m, dict) else None

    async def photo(self, chat, url, caption):
        m = await self._call("sendPhoto", chat_id=chat, photo=url, caption=(caption or "")[:1024] or None)
        return m.get("message_id") if isinstance(m, dict) else None

    async def finish_menu(self, chat, message_id, note, remove):
        from .telegram import TelegramError
        if remove:
            try:
                await self._call("editMessageReplyMarkup", chat_id=chat, message_id=message_id,
                                 reply_markup={"inline_keyboard": []})
            except TelegramError:
                pass

    async def answer_callback(self, cq_id, text=""):
        from .telegram import TelegramError
        try:
            await self._call("answerCallbackQuery", callback_query_id=cq_id, text=text or None)
        except TelegramError:
            pass

    async def invoice(self, chat, title, description, payload, currency, amount, provider_token):
        m = await self._call("sendInvoice", chat_id=chat, title=title[:32] or "Оплата",
                             description=description[:255] or title[:255] or "Оплата", payload=payload,
                             provider_token=provider_token if currency != "XTR" else "", currency=currency,
                             prices=[{"label": title[:32] or "Оплата", "amount": amount}])
        return m.get("message_id") if isinstance(m, dict) else None

    async def answer_pre_checkout(self, qid, ok, error=None):
        from .telegram import TelegramError
        try:
            await self._call("answerPreCheckoutQuery", pre_checkout_query_id=qid, ok=ok,
                             error_message=None if ok else (error or "Счёт устарел"))
        except TelegramError:
            pass

    async def set_commands(self, commands, chat_id=None):
        from .telegram import TelegramError
        try:
            await self._call("setMyCommands", commands=commands[:100],
                             scope={"type": "chat", "chat_id": chat_id} if chat_id else None)
        except TelegramError:
            pass


def _fmt_amount(amount: int, currency: str) -> str:
    return f"{amount} ⭐" if currency == "XTR" else f"{amount / 100:.2f} {currency}"


def to_minor(value: float, currency: str) -> int:
    """Сумма для Telegram: звёзды — целые, валюта — в копейках/центах."""
    return int(round(value)) if currency == "XTR" else int(round(value * 100))


# ---------- движок ----------

class FlowError(Exception):
    def __init__(self, node, message):
        super().__init__(message)
        self.node = node


class _Stop(Exception):
    pass


@dataclass
class Waiter:
    kind: str                       # text | menu
    future: asyncio.Future
    message_id: int | None = None
    buttons: list[dict] = field(default_factory=list)


class BotEngine:
    def __init__(self, pid: str, compiled: CompiledBot, transport, store: BotStore, hub=None):
        self.pid, self.compiled, self.transport, self.store, self.hub = pid, compiled, transport, store, hub
        self.waiters: dict[str, Waiter] = {}
        self.tasks: set[asyncio.Task] = set()
        self.busy = 0
        self.log: deque = deque(maxlen=300)
        self.stats = {"in": 0, "out": 0, "errors": 0}

    def event(self, kind, chat, text, node=None, user=None):
        self.log.appendleft({"t": time.time(), "kind": kind, "chat": str(chat), "user": user, "text": str(text)[:500],
                             "node": node})
        if kind in ("in", "out", "error"):
            self.stats[{"in": "in", "out": "out", "error": "errors"}[kind]] += 1

    async def handle(self, upd: dict):
        """upd: {chat, user:{id,name,username}} + text | callback{id,data,message_id} | pre_checkout | payment."""
        chat, user = str(upd["chat"]), upd["user"]
        key = f"{chat}:{user['id']}"
        if upd.get("pre_checkout"):
            await self._pre_checkout(upd["pre_checkout"])
            return
        saved = self.store.touch(user, chat)
        role = self.store.role(user["id"], self.compiled.admins)
        if upd.get("payment"):
            self._paid(chat, user, upd["payment"])
            return
        if role == "banned":
            self.event("in", chat, "[заблокирован] " + str(upd.get("text") or ""), user=user.get("name"))
            return
        cb = upd.get("callback")
        if cb:
            self.event("in", chat, f"[кнопка {cb.get('data')}]", user=user.get("name"))
            w = self.waiters.get(key)
            if w and w.kind == "menu" and w.message_id == cb.get("message_id") and not w.future.done():
                w.future.set_result(cb.get("data"))
                await self.transport.answer_callback(cb.get("id"))
            else:
                await self.transport.answer_callback(cb.get("id"), "Это меню уже неактуально")
            return
        text = str(upd.get("text") or "")
        self.event("in", chat, text, user=user.get("name"))
        route, args = self.compiled.route(text)
        w = self.waiters.get(key)
        if w and not w.future.done():
            is_command = text.startswith("/") and route is not None and route.kind == "command"
            if is_command:
                w.future.cancel()                      # команда прерывает незаконченный диалог
            elif w.kind == "text":
                w.future.set_result(text)
                return
            else:
                hit = next((b["id"] for b in w.buttons if b["text"].strip().lower() == text.strip().lower()), None)
                if hit:
                    w.future.set_result(hit)
                    return
        if route is None:
            return
        if route.roles and role != "admin" and role not in route.roles:
            await self._send(chat, self.compiled.deny_text, route.node)
            return
        ctx = {"text": text, "args": args, "chat_id": chat, "user_id": str(user["id"]),
               "user_name": user.get("name") or "", "username": user.get("username") or "", "saved": saved,
               "role": role, "is_admin": role == "admin"}
        task = asyncio.ensure_future(self._run(route, ctx, key))
        self.tasks.add(task)
        task.add_done_callback(self.tasks.discard)

    async def _run(self, route: Route, ctx: dict, key: str):
        self.busy += 1
        try:
            await self.block(route.steps, ctx, key)
        except (_Stop, asyncio.CancelledError):
            pass
        except FlowError as e:
            self.event("error", ctx["chat_id"], str(e), e.node)
        except Exception as e:  # noqa: BLE001 — ошибка одного диалога не роняет бота
            self.event("error", ctx["chat_id"], f"{type(e).__name__}: {e}")
        finally:
            self.busy -= 1

    def stop(self):
        for t in list(self.tasks):
            t.cancel()

    # --- оплата ---

    async def _pre_checkout(self, q: dict):
        """Telegram спрашивает «можно оплатить?» — ответить нужно за 10 секунд."""
        w = self.waiters.get(f"pay:{q.get('payload')}")
        ok = bool(w and not w.future.done() and w.buttons and w.buttons[0].get("amount") == q.get("total_amount")
                  and w.buttons[0].get("currency") == q.get("currency"))
        await self.transport.answer_pre_checkout(q.get("id"), ok, None if ok else "Счёт устарел — запросите новый")

    def _paid(self, chat, user, p: dict):
        record = {"t": time.time(), "user_id": str(user["id"]), "user_name": user.get("name"), "chat": chat,
                  "amount": p.get("total_amount"), "currency": p.get("currency"), "payload": p.get("payload"),
                  "charge_id": p.get("charge_id")}
        self.store.add_payment(record)
        self.event("in", chat, f"[оплата {_fmt_amount(p.get('total_amount') or 0, p.get('currency') or '')}]",
                   user=user.get("name"))
        w = self.waiters.get(f"pay:{p.get('payload')}")
        if w and not w.future.done():
            w.future.set_result(record)
        else:
            self.event("error", chat, "оплата пришла, но диалог уже не ждёт её (таймаут или перезапуск) — "
                                      "проверьте вкладку «Платежи»")

    # --- шаги ---

    def _text(self, template, ctx, node, what):
        try:
            return render_template(template, ctx, self.compiled.env_values)
        except expr.ExprError as e:
            raise FlowError(node, f"{what}: {e}") from None

    def _ev(self, source, ctx, node, what):
        try:
            return expr.evaluate(expr.parse(str(source)), ctx, self.compiled.env_values)
        except expr.ExprError as e:
            raise FlowError(node, f"{what}: {e}") from None

    def _num(self, source, ctx, node, what, default):
        if source in (None, ""):
            return default
        v = self._ev(source, ctx, node, what)
        try:
            return float(v)
        except (TypeError, ValueError):
            raise FlowError(node, f"{what}: ожидалось число") from None

    async def _wait(self, key, waiter: Waiter, timeout):
        old = self.waiters.get(key)
        if old and not old.future.done():
            old.future.cancel()
        self.waiters[key] = waiter
        self.busy -= 1
        try:
            return await asyncio.wait_for(waiter.future, timeout)
        except asyncio.TimeoutError:
            return None
        finally:
            self.busy += 1
            if self.waiters.get(key) is waiter:
                del self.waiters[key]

    async def _send(self, chat, text, node, **kw):
        from .telegram import TelegramError
        try:
            mid = await self.transport.send(chat, text, **kw)
        except TelegramError as e:
            raise FlowError(node, f"Telegram: {e}") from None
        self.event("out", chat, text, node)
        return mid

    async def block(self, steps, ctx, key):
        for s in steps:
            node, op = s["node"], s["op"]
            chat = ctx["chat_id"]
            if op == "bot_send":
                text = self._text(s["text"], ctx, node, "текст")
                to = s.get("to") or "current"
                if to == "role":
                    sent = await self._broadcast(s.get("role") or "all", text, node, s.get("links"))
                    if s.get("count_var"):
                        ctx[s["count_var"]] = sent
                else:
                    target = self._text(s.get("chat") or "", ctx, node, "чат").strip() if to == "chat" else chat
                    await self._send(target or chat, text, node, links=s.get("links"))
            elif op == "bot_role":
                target = self._text(s.get("target") or "", ctx, node, "кому").strip() or ctx["user_id"]
                found = self.store.find(target)
                if found is None:
                    await self.block(s["branches"]["not_found"], ctx, key)
                    return
                uid, _ = found
                if uid in self.compiled.admins:
                    raise FlowError(node, "роль администратора из настроек нельзя изменить блоком")
                u = self.store.set_role(uid, s["role"])
                self.event("out", chat, f"[роль] {u.get('name') or uid} → {s['role']}", node)
                if s.get("var"):
                    ctx[s["var"]] = {"id": uid, "name": u.get("name"), "username": u.get("username"),
                                     "role": s["role"]}
                if uid == ctx["user_id"]:
                    ctx["role"] = s["role"]
                manager_refresh_commands(self)
                await self.block(s["branches"]["ok"], ctx, key)
                return
            elif op == "bot_invoice":
                currency = s.get("currency") or "XTR"
                value = self._num(s.get("amount"), ctx, node, "сумма", 0)
                amount = to_minor(value, currency)
                if amount <= 0:
                    raise FlowError(node, "сумма должна быть больше нуля")
                if currency == "XTR" and amount > 100000:
                    raise FlowError(node, "не больше 100 000 звёзд")
                payload = f"{node}:{uuid.uuid4().hex[:12]}"
                title = self._text(s.get("title") or "", ctx, node, "название")[:32]
                desc = self._text(s.get("description") or "", ctx, node, "описание")[:255]
                from .telegram import TelegramError
                try:
                    await self.transport.invoice(chat, title, desc, payload, currency, amount,
                                                 self.compiled.provider_token)
                except TelegramError as e:
                    raise FlowError(node, f"Telegram: {e}") from None
                self.event("out", chat, f"[счёт] {title}: {_fmt_amount(amount, currency)}", node)
                timeout = min(self._num(s.get("timeout"), ctx, node, "ожидание", 900), WAIT_LIMIT)
                w = Waiter("payment", asyncio.get_running_loop().create_future(),
                           buttons=[{"amount": amount, "currency": currency}])
                paid = await self._wait(f"pay:{payload}", w, timeout)
                if s.get("var"):
                    ctx[s["var"]] = None if paid is None else {
                        "amount": int(value) if float(value).is_integer() else value, "currency": currency,
                        "charge_id": paid.get("charge_id")}
                await self.block(s["branches"]["paid" if paid else "timeout"], ctx, key)
                return
            elif op == "bot_photo":
                from .telegram import TelegramError
                url = self._text(s["url"], ctx, node, "адрес")
                try:
                    await self.transport.photo(chat, url, self._text(s.get("caption") or "", ctx, node, "подпись"))
                except TelegramError as e:
                    raise FlowError(node, f"Telegram: {e}") from None
                self.event("out", chat, f"[фото] {url}", node)
            elif op == "bot_ask":
                await self._send(chat, self._text(s["text"], ctx, node, "вопрос"), node)
                timeout = min(self._num(s.get("timeout"), ctx, node, "ожидание", 3600), WAIT_LIMIT)
                branch = "timeout"
                for _ in range(5):
                    answer = await self._wait(key, Waiter("text", asyncio.get_running_loop().create_future()), timeout)
                    if answer is None:
                        break
                    value, good = answer, True
                    if s.get("type") in ("number", "int"):
                        try:
                            num = float(answer.replace(",", ".").strip())
                            value = int(num) if s["type"] == "int" and num.is_integer() else num
                            good = s["type"] != "int" or float(num).is_integer()
                        except ValueError:
                            good = False
                    if good:
                        ctx[s["var"]] = value
                        branch = "ok"
                        break
                    await self._send(chat, self._text(s.get("retry") or "Не понял, попробуйте ещё раз", ctx, node,
                                                      "повтор"), node)
                await self.block(s["branches"].get(branch, []), ctx, key)
                return
            elif op == "bot_menu":
                text = self._text(s["text"], ctx, node, "текст")
                mid = await self._send(chat, text, node, buttons=s["buttons"])
                timeout = min(self._num(s.get("timeout"), ctx, node, "ожидание", 3600), WAIT_LIMIT)
                choice = await self._wait(key, Waiter("menu", asyncio.get_running_loop().create_future(), mid,
                                                      s["buttons"]), timeout)
                label = next((b["text"] for b in s["buttons"] if b["id"] == choice), None)
                if s.get("var"):
                    ctx[s["var"]] = label
                await self.transport.finish_menu(chat, mid, f"→ {label}" if label else "⌛", s.get("remove", True))
                await self.block(s["branches"].get(choice or "timeout", []), ctx, key)
                return
            elif op == "condition":
                await self.block(s["branches"]["true" if self._ev(s["expr"], ctx, node, "условие") else "false"],
                                 ctx, key)
                return
            elif op == "set_vars":
                for a in s["assignments"]:
                    ctx[a["target"]] = self._ev(a["expr"], ctx, node, a["target"])
            elif op == "bot_save":
                values = {a["target"]: self._ev(a["expr"], ctx, node, a["target"]) for a in s["assignments"]}
                ctx["saved"] = self.store.save(ctx["user_id"], values)
            elif op == "wait":
                await asyncio.sleep(max(0, min(self._num(s.get("seconds"), ctx, node, "пауза", 1), 60)))
            elif op == "stop":
                raise _Stop()
            elif op == "code":
                spec = self.compiled.code[node]
                from .pyenv import envs
                r = await sandbox.run_code(spec["code"], jsonable(ctx), self.compiled.env_values,
                                           sandbox.Limits(spec["timeout"], spec["memory_mb"]), self.compiled.sandbox,
                                           envs.venv(self.pid), self.compiled.firewall)
                if not r.ok:
                    raise FlowError(node, f"Python-код: {r.error}")
                try:
                    ctx.update(apply_code_result(r.result, spec["outputs"]))
                except ValueError as e:
                    raise FlowError(node, f"Python-код: {e}") from None
            elif op == "http":
                from .agents import HTTP_CODE
                spec = self.compiled.http[node]
                if sandbox.egress_mode(self.compiled.sandbox, self.compiled.firewall) == "off":
                    raise FlowError(node, "сеть для проекта закрыта — разрешите адрес в «Настройки → Firewall»")
                url = self._text(s["url"], ctx, node, "адрес")
                if not url.startswith(("http://", "https://")):
                    raise FlowError(node, "адрес должен начинаться с http:// или https://")
                body = {b["key"]: jsonable(self._ev(b["expr"], ctx, node, b["key"])) for b in s.get("body") or []}
                r = await sandbox.run_code(HTTP_CODE, {"url": url, "method": spec["method"], "body": body,
                                                       "timeout": spec["timeout"]}, {},
                                           sandbox.Limits(min(spec["timeout"] + 2, 10), 128),
                                           self.compiled.sandbox, None, self.compiled.firewall)
                if not r.ok:
                    raise FlowError(node, f"HTTP-запрос: {r.error}")
                ctx[s.get("target") or "response"] = (r.result or {}).get("response")
            elif op == "run_scenario":
                result = await self._scenario(s, ctx, node)
                if s.get("target"):
                    ctx[s["target"]] = result
            else:
                raise FlowError(node, f"неизвестный шаг «{op}»")

    async def _broadcast(self, role, text, node, links=None) -> int:
        """Рассылка: не быстрее ~20 сообщений в секунду (лимит Telegram — 30)."""
        from .telegram import TelegramError
        sent = 0
        for uid in self.store.by_role(role, self.compiled.admins):
            try:
                await self.transport.send(uid, text, links=links)
                sent += 1
            except TelegramError as e:
                self.event("error", uid, f"рассылка: {e}", node)
            await asyncio.sleep(0.05)
        self.event("out", f"роль {role}", f"[рассылка {sent}] {text}", node)
        return sent

    async def _scenario(self, s, ctx, node):
        hub = self.hub
        if hub is None:
            raise FlowError(node, "сценарии недоступны")
        compiled = hub.enabled.get(s["project"])
        if compiled is None:
            raise FlowError(node, "сценарии проекта-автоматизации выключены или проект не найден")
        sc = compiled.scenario(s["scenario"])
        if sc is None or sc.trigger.kind != "webhook":
            raise FlowError(node, f"нет сценария «{s['scenario']}» с запуском «Вызов по HTTP»")
        from . import auth
        if auth.enabled():
            owner = (auth.store.acl().get(self.pid) or {}).get("owner")
            user = auth.store.get_user(owner) if owner else None
            if auth.store.role_for(user, s["project"]) not in ("owner", "editor"):
                raise FlowError(node, "владелец бота не имеет прав на проект-автоматизацию")
        params = {b["key"]: jsonable(self._ev(b["expr"], ctx, node, b["key"])) for b in s.get("params") or []}
        try:
            run = await hub.start_run(s["project"], compiled, s["scenario"], params, "bot", hub.enabled_by.get(s["project"]))
        except (ValueError, LookupError) as e:
            raise FlowError(node, f"сценарий не запущен: {e}") from None
        if not s.get("wait", True):
            return {"status": "running", "run_id": run.id}
        try:
            await asyncio.wait_for(run.done.wait(), 600)
        except asyncio.TimeoutError:
            return {"status": "running", "run_id": run.id}
        return {"status": run.status, "message": run.message, "vars": run.vars, "run_id": run.id}


# ---------- запущенные боты ----------

@dataclass
class LiveBot:
    pid: str
    engine: BotEngine
    token: str
    username: str = ""
    task: asyncio.Task | None = None
    error: str | None = None
    started: float = field(default_factory=time.time)
    offset: int = 0


async def sync_commands(engine: "BotEngine"):
    """Меню команд: общее — для всех, команды для ролей — только в чатах тех, у кого роль есть."""
    compiled = engine.compiled
    item = lambda c, r: {"command": c, "description": (r.description or c)[:250]}  # noqa: E731
    public = [item(c, r) for c, r in compiled.commands.items() if not r.roles]
    await engine.transport.set_commands(public)
    if not any(r.roles for r in compiled.commands.values()):
        return
    privileged = {uid: engine.store.role(uid, compiled.admins) for uid in
                  set(compiled.admins) | {u["id"] for u in engine.store.users() if u.get("role") not in (None, "banned")}}
    for uid, role in privileged.items():
        cmds = [item(c, r) for c, r in compiled.commands.items()
                if not r.roles or role == "admin" or role in r.roles]
        await engine.transport.set_commands(cmds, chat_id=uid)


def manager_refresh_commands(engine: "BotEngine"):
    """После смены роли — обновить меню команд (в фоне)."""
    asyncio.ensure_future(sync_commands(engine))


def normalize_update(upd: dict) -> dict | None:
    if "callback_query" in upd:
        cq = upd["callback_query"]
        msg = cq.get("message") or {}
        u = cq.get("from") or {}
        return {"chat": (msg.get("chat") or {}).get("id"), "user": _user(u),
                "callback": {"id": cq.get("id"), "data": cq.get("data"), "message_id": msg.get("message_id")}}
    if "pre_checkout_query" in upd:
        q = upd["pre_checkout_query"]
        return {"chat": (q.get("from") or {}).get("id"), "user": _user(q.get("from") or {}),
                "pre_checkout": {"id": q.get("id"), "currency": q.get("currency"), "total_amount": q.get("total_amount"),
                                 "payload": q.get("invoice_payload")}}
    msg = upd.get("message")
    if msg and msg.get("successful_payment"):
        p = msg["successful_payment"]
        return {"chat": (msg.get("chat") or {}).get("id"), "user": _user(msg.get("from") or {}),
                "payment": {"currency": p.get("currency"), "total_amount": p.get("total_amount"),
                            "payload": p.get("invoice_payload"), "charge_id": p.get("telegram_payment_charge_id")}}
    if not msg or "text" not in msg:
        return None
    return {"chat": (msg.get("chat") or {}).get("id"), "user": _user(msg.get("from") or {}), "text": msg["text"]}


def _user(u: dict) -> dict:
    return {"id": u.get("id"), "name": " ".join(x for x in (u.get("first_name"), u.get("last_name")) if x),
            "username": u.get("username") or ""}


class BotManager:
    def __init__(self):
        self.live: dict[str, LiveBot] = {}
        self.emulators: dict[str, BotEngine] = {}
        self.hub = None
        self.loop: asyncio.AbstractEventLoop | None = None

    @staticmethod
    def state_path() -> Path:
        return BOTS_DIR / "running.json"

    def _persist(self):
        BOTS_DIR.mkdir(parents=True, exist_ok=True)
        self.state_path().write_text(json.dumps(sorted(self.live)), "utf-8")

    def store(self, pid: str) -> BotStore:
        live = self.live.get(pid)
        return live.engine.store if live else BotStore(BOTS_DIR / f"{pid}.json")

    def uses_token(self, token: str) -> bool:
        return any(b.token == token for b in self.live.values())

    @staticmethod
    def compiled_for(pid: str, project: dict | None = None) -> CompiledBot:
        project = project or storage.load(pid)
        return compile_bot({**project, "env": vault.unseal_env(project.get("env"), project.get("env"))})

    async def start(self, pid: str) -> LiveBot:
        from .telegram import TelegramError, call
        compiled = self.compiled_for(pid)
        token = compiled.token
        if not token:
            raise ValueError("не задан токен бота: «Настройки проекта → Бот»")
        other = next((b for b in self.live.values() if b.token == token and b.pid != pid), None)
        if other:
            raise ValueError(f"этот токен уже использует бот проекта {other.pid}")
        if self.hub and token in self.hub.telegram.pollers:
            raise ValueError("этот токен уже используется в настройках Telegram проекта-автоматизации")
        try:
            me = await call(token, "getMe")
        except TelegramError as e:
            raise ValueError(f"Telegram: {e}") from None
        await self.stop(pid, persist=False)
        engine = BotEngine(pid, compiled, TelegramTransport(token), BotStore(BOTS_DIR / f"{pid}.json"), self.hub)
        bot = LiveBot(pid, engine, token, me.get("username") or "")
        self.live[pid] = bot
        bot.task = asyncio.ensure_future(self._poll(bot))
        self._persist()
        asyncio.ensure_future(self._commands(bot))
        return bot

    async def _commands(self, bot: LiveBot):
        await sync_commands(bot.engine)

    async def stop(self, pid: str, persist: bool = True):
        bot = self.live.pop(pid, None)
        if bot:
            if bot.task:
                bot.task.cancel()
            bot.engine.stop()
        if persist:
            self._persist()

    def reload(self, pid: str, project: dict):
        """Проект сохранили: работающий бот подхватывает новую версию (идущие диалоги доживают по старой)."""
        bot = self.live.get(pid)
        if bot is None:
            return None
        try:
            compiled = self.compiled_for(pid, project)
        except (GraphError, vault.VaultError) as e:
            bot.error = f"новая версия не применена: {e}"
            return bot.error
        if compiled.token != bot.token:
            bot.error = "токен изменился — перезапустите бота"
            return bot.error
        bot.engine.compiled = compiled
        bot.error = None
        if self.loop:
            asyncio.run_coroutine_threadsafe(self._commands(bot), self.loop)
        return None

    async def _poll(self, bot: LiveBot):
        from .telegram import POLL_TIMEOUT, TelegramError, call
        backoff = 1
        while True:
            began = time.monotonic()
            try:
                updates = await call(bot.token, "getUpdates", http_timeout=POLL_TIMEOUT, timeout=POLL_TIMEOUT,
                                     offset=bot.offset or None, allowed_updates=["message", "callback_query", "pre_checkout_query"])
                bot.error = None if not (bot.error or "").startswith("новая версия") else bot.error
                backoff = 1
            except asyncio.CancelledError:
                raise
            except TelegramError as e:
                bot.error = str(e)
                await asyncio.sleep(backoff)
                backoff = min(backoff * 2, 60)
                continue
            for upd in updates or []:
                bot.offset = max(bot.offset, int(upd.get("update_id", 0)) + 1)
                norm = normalize_update(upd)
                if norm and norm["chat"] is not None and norm["user"]["id"] is not None:
                    try:
                        await bot.engine.handle(norm)
                    except Exception as e:  # noqa: BLE001
                        bot.engine.event("error", norm["chat"], f"{type(e).__name__}: {e}")
            if not updates and time.monotonic() - began < 0.5:
                await asyncio.sleep(1)

    def status(self, pid: str) -> dict:
        bot = self.live.get(pid)
        if bot is None:
            return {"running": False}
        return {"running": True, "username": bot.username, "error": bot.error, "started": bot.started,
                "stats": bot.engine.stats, "dialogs": len(bot.engine.waiters)}

    async def autostart(self):
        try:
            pids = json.loads(self.state_path().read_text("utf-8"))
        except (OSError, ValueError):
            return
        for pid in pids:
            try:
                await self.start(pid)
            except Exception as e:  # noqa: BLE001
                print(f"DragAPI: бот проекта {pid} не запущен: {e}")
        self._persist()

    def stop_all(self):
        for bot in self.live.values():
            if bot.task:
                bot.task.cancel()
            bot.engine.stop()
        self.live.clear()

    # --- тест-чат ---

    def emulator(self, pid: str, compiled: CompiledBot, reset: bool = False) -> BotEngine:
        eng = self.emulators.get(pid)
        if eng is None or reset:
            if eng:
                eng.stop()
            eng = BotEngine(pid, compiled, EmulatorTransport(), BotStore(None), self.hub)
            self.emulators[pid] = eng
        eng.compiled = compiled
        return eng


EMU_CHAT = "1"
EMU_USER = {"id": 1, "name": "Вы", "username": "tester"}


async def emulate(engine: BotEngine, text: str | None = None, button: dict | None = None, wait: float = 4,
                  pay: dict | None = None):
    tr: EmulatorTransport = engine.transport
    if pay is not None:
        msg = next((m for m in tr.messages if m["id"] == pay.get("message_id") and m.get("invoice")), None)
        if msg is None:
            return
        inv = msg["invoice"]
        tr.user_message(f"[оплатить {_fmt_amount(inv['amount'], inv['currency'])}]")
        w = engine.waiters.get(f"pay:{inv['payload']}")
        if w is None or w.future.done():
            msg["note"] = "счёт устарел"
            return
        msg["note"] = "оплачено (тест)"
        await engine.handle({"chat": EMU_CHAT, "user": EMU_USER, "payment": {
            "currency": inv["currency"], "total_amount": inv["amount"], "payload": inv["payload"],
            "charge_id": "test-" + uuid.uuid4().hex[:8]}})
    elif text is not None:
        tr.user_message(text)
        await engine.handle({"chat": EMU_CHAT, "user": EMU_USER, "text": text})
    elif button is not None:
        tr.user_message(f"[{button.get('text') or button.get('data')}]")
        await engine.handle({"chat": EMU_CHAT, "user": EMU_USER,
                             "callback": {"id": uuid.uuid4().hex, "data": button.get("data"),
                                          "message_id": button.get("message_id")}})
    end = time.monotonic() + wait
    await asyncio.sleep(0.02)
    while engine.busy > 0 and time.monotonic() < end:
        await asyncio.sleep(0.05)


manager = BotManager()
_ = (Any, os, _check_expr, _check_ident)
