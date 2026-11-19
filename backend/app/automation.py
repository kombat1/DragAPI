"""Проекты-автоматизации: каталог блоков, компиляция сценариев и превращение записи действий в блоки.

Граф автоматизации — тот же формат React Flow, что у API-проектов. Каждый блок «Старт» начинает
свой сценарий; компиляция превращает цепочку в план (JSON) для агента (dragapi_agent/runner.py).
Ветвления — «Условие» (да/нет), циклы — «Повторить» и «Пока» (выходы «тело» и «дальше»): тело
заканчивается там, где обрывается цепочка, или связью обратно в блок цикла.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Any

from dragapi_agent.keys import KeyError_, parse_combo

from . import expr
from .firewall import FirewallSettings
from .graph import EnvVar, GraphError, Issue, _check_ident, _compile_env, check_code_block
from .pyenv import validate_packages
from .sandbox import SandboxSettings

TRIGGER_KINDS = ["manual", "hotkey", "interval", "daily", "webhook", "telegram"]
TG_COMMAND_RE = re.compile(r"^[a-z0-9_]{1,32}$")
TG_CHAT_RE = re.compile(r"^-?\d{1,20}$")
TG_BUILTIN = {"start", "help", "list", "status", "stop", "id"}
TG_NOTIFY = ["never", "errors", "always"]
WEEKDAYS = ["mon", "tue", "wed", "thu", "fri", "sat", "sun"]
BUTTONS = ["left", "right", "middle"]
TEMPLATE_RE = re.compile(r"\{\{|\}\}|\{([^{}]+)\}")
TIME_RE = re.compile(r"^([01]\d|2[0-3]):[0-5]\d$")
NAME_RE = re.compile(r"^[A-Za-z0-9_-]{1,40}$")
MAX_STEPS = 5000

# Поля блока описывают и форму в инспекторе (фронтенд строит её по каталогу), и проверку при компиляции.
#   expr — выражение; num — выражение-число (можно просто 10); template — текст с {выражениями};
#   select/bool/text/keys/assignments/kv/code/strings — как в API-блоках.
F = dict


def _win(hint="Имя переменной из блока «Найти окно» (например, win): координаты отсчитываются от левого верхнего "
                   "угла окна — сценарий не сломается, если окно сдвинут"):
    return F(key="window", label="Внутри окна", kind="window", optional=True, hint=hint)


def _xy(prefix="", hint="Пусто — текущая позиция курсора"):
    return [F(key=f"x{prefix}", label=f"X{prefix}", kind="num", optional=True, group="point", hint=hint),
            F(key=f"y{prefix}", label=f"Y{prefix}", kind="num", optional=True, group="point")]


AUTO_BLOCKS: dict[str, dict] = {
    "trigger": {
        "title": "Старт", "group": "Запуск", "color": "#6366f1", "icon": "⚑",
        "description": "Начало сценария: вручную, по горячей клавише, по расписанию или по вызову из API.",
        "inputs": [], "output": ["flow"],
        "defaults": {"name": "main", "kind": "manual", "hotkey": "ctrl+alt+1", "every_seconds": "60",
                     "at": "09:00", "weekdays": [], "params": [], "command": ""},
        "fields": [
            F(key="name", label="Имя сценария", kind="text", hint="Латиница, цифры, - и _; по нему сценарий вызывают из API"),
            F(key="kind", label="Запуск", kind="select", options=[
                ["manual", "Только вручную"], ["hotkey", "Горячая клавиша"], ["interval", "Каждые N секунд"],
                ["daily", "Каждый день в…"], ["webhook", "Вызов по HTTP (из API-проекта)"],
                ["telegram", "Команда Telegram-бота"]]),
            F(key="command", label="Команда бота", kind="text", show_if={"kind": ["telegram"]},
              hint="Латиница в нижнем регистре, цифры и _: например click → в чате /click. Параметры: "
                   "/click n=5 или /click 5"),
            F(key="hotkey", label="Сочетание клавиш", kind="keys", show_if={"kind": ["hotkey"]}),
            F(key="every_seconds", label="Интервал, с", kind="text", show_if={"kind": ["interval"]},
              hint="Не меньше 5 секунд"),
            F(key="at", label="Время (ЧЧ:ММ)", kind="text", show_if={"kind": ["daily"]}),
            F(key="weekdays", label="Дни недели", kind="weekdays", show_if={"kind": ["daily"]},
              hint="Ничего не выбрано — каждый день"),
            F(key="params", label="Входные параметры", kind="strings", show_if={"kind": ["webhook", "manual", "telegram"]},
              hint="Имена полей, которые передают при запуске; в сценарии — переменные"),
        ],
    },
    "mouse_move": {
        "title": "Переместить мышь", "group": "Мышь", "color": "#0ea5e9", "icon": "🖱",
        "description": "Перевести курсор в точку (или сместить относительно текущей позиции).",
        "inputs": ["flow"], "output": ["flow"],
        "defaults": {"x": "500", "y": "300", "relative": False, "duration": "0.2"},
        "fields": [*_xy(hint=""), F(key="relative", label="Относительно текущей позиции", kind="bool"),
                   F(key="duration", label="Длительность, с", kind="num", optional=True),
                   {**_win(), "show_if": {"relative": [False]}}],
        "required": ["x", "y"],
    },
    "mouse_click": {
        "title": "Клик", "group": "Мышь", "color": "#0ea5e9", "icon": "👆",
        "description": "Щелчок кнопкой мыши — в точке или там, где сейчас курсор.",
        "inputs": ["flow"], "output": ["flow"],
        "defaults": {"x": "", "y": "", "button": "left", "clicks": "1", "duration": "0"},
        "fields": [*_xy(), F(key="button", label="Кнопка", kind="select",
                              options=[["left", "Левая"], ["right", "Правая"], ["middle", "Средняя"]]),
                   F(key="clicks", label="Щелчков (1–3)", kind="num", optional=True),
                   F(key="duration", label="Перемещение к точке, с", kind="num", optional=True), _win()],
    },
    "mouse_down": {
        "title": "Зажать кнопку", "group": "Мышь", "color": "#0ea5e9", "icon": "⤓",
        "description": "Нажать кнопку мыши и не отпускать (до блока «Отпустить кнопку»).",
        "inputs": ["flow"], "output": ["flow"],
        "defaults": {"x": "", "y": "", "button": "left", "duration": "0"},
        "fields": [*_xy(), F(key="button", label="Кнопка", kind="select",
                              options=[["left", "Левая"], ["right", "Правая"], ["middle", "Средняя"]]),
                   F(key="duration", label="Перемещение к точке, с", kind="num", optional=True), _win()],
    },
    "mouse_up": {
        "title": "Отпустить кнопку", "group": "Мышь", "color": "#0ea5e9", "icon": "⤒",
        "description": "Отпустить зажатую кнопку мыши.",
        "inputs": ["flow"], "output": ["flow"],
        "defaults": {"x": "", "y": "", "button": "left", "duration": "0"},
        "fields": [*_xy(), F(key="button", label="Кнопка", kind="select",
                              options=[["left", "Левая"], ["right", "Правая"], ["middle", "Средняя"]]),
                   F(key="duration", label="Перемещение к точке, с", kind="num", optional=True), _win()],
    },
    "mouse_drag": {
        "title": "Перетащить", "group": "Мышь", "color": "#0ea5e9", "icon": "✋",
        "description": "Зажать кнопку в одной точке, провести и отпустить в другой.",
        "inputs": ["flow"], "output": ["flow"],
        "defaults": {"x1": "", "y1": "", "x2": "800", "y2": "400", "button": "left", "duration": "0.3"},
        "fields": [F(key="x1", label="Откуда X", kind="num", optional=True, group="point",
                     hint="Пусто — от текущей позиции"),
                   F(key="y1", label="Откуда Y", kind="num", optional=True, group="point"),
                   F(key="x2", label="Куда X", kind="num", group="point"),
                   F(key="y2", label="Куда Y", kind="num", group="point"),
                   F(key="button", label="Кнопка", kind="select",
                     options=[["left", "Левая"], ["right", "Правая"], ["middle", "Средняя"]]),
                   F(key="duration", label="Длительность, с", kind="num", optional=True), _win()],
    },
    "mouse_scroll": {
        "title": "Прокрутка", "group": "Мышь", "color": "#0ea5e9", "icon": "↕",
        "description": "Колесо мыши: отрицательное — вниз, положительное — вверх.",
        "inputs": ["flow"], "output": ["flow"],
        "defaults": {"dy": "-3", "dx": "0", "x": "", "y": ""},
        "fields": [F(key="dy", label="По вертикали (щелчков)", kind="num", optional=True),
                   F(key="dx", label="По горизонтали (щелчков)", kind="num", optional=True), *_xy(), _win()],
    },
    "autoclick": {
        "title": "Автокликер", "group": "Мышь", "color": "#0284c7", "icon": "⚡",
        "description": "Много щелчков с заданным интервалом: N раз или в течение времени.",
        "inputs": ["flow"], "output": ["flow"],
        "defaults": {"x": "", "y": "", "button": "left", "interval_ms": "100", "mode": "count", "count": "10",
                     "seconds": "5", "target": ""},
        "fields": [*_xy(), F(key="button", label="Кнопка", kind="select",
                              options=[["left", "Левая"], ["right", "Правая"], ["middle", "Средняя"]]),
                   F(key="interval_ms", label="Интервал, мс (от 10)", kind="num"),
                   F(key="mode", label="Сколько", kind="select",
                     options=[["count", "N щелчков"], ["seconds", "В течение N секунд"]]),
                   F(key="count", label="Щелчков", kind="num", show_if={"mode": ["count"]}),
                   F(key="seconds", label="Секунд", kind="num", show_if={"mode": ["seconds"]}),
                   F(key="target", label="Записать число щелчков в переменную", kind="text", optional=True), _win()],
    },
    "key_type": {
        "title": "Ввести текст", "group": "Клавиатура", "color": "#f59e0b", "icon": "⌨",
        "description": "Напечатать текст. {выражение} подставляет значение: «Заказ {n}».",
        "inputs": ["flow"], "output": ["flow"],
        "defaults": {"text": "Привет!", "interval_ms": "20", "enter": False},
        "fields": [F(key="text", label="Текст", kind="template", multiline=True),
                   F(key="interval_ms", label="Пауза между символами, мс", kind="num", optional=True),
                   F(key="enter", label="Нажать Enter в конце", kind="bool")],
    },
    "key_press": {
        "title": "Нажать клавиши", "group": "Клавиатура", "color": "#f59e0b", "icon": "⌘",
        "description": "Клавиша или сочетание: ctrl+c, alt+tab, enter, f5.",
        "inputs": ["flow"], "output": ["flow"],
        "defaults": {"keys": "ctrl+c", "action": "tap", "times": "1"},
        "fields": [F(key="keys", label="Сочетание", kind="keys"),
                   F(key="action", label="Действие", kind="select",
                     options=[["tap", "Нажать и отпустить"], ["down", "Зажать"], ["up", "Отпустить"]]),
                   F(key="times", label="Сколько раз", kind="num", optional=True, show_if={"action": ["tap"]})],
    },
    "wait": {
        "title": "Пауза", "group": "Логика", "color": "#64748b", "icon": "⏱",
        "description": "Подождать N секунд (можно дробное: 0.5).",
        "inputs": ["flow"], "output": ["flow"],
        "defaults": {"seconds": "1"},
        "fields": [F(key="seconds", label="Секунд", kind="num")],
    },
    "repeat": {
        "title": "Повторить", "group": "Логика", "color": "#10b981", "icon": "🔁",
        "description": "Выполнить «тело» N раз, затем идти «дальше». Номер повтора (с 1) — в переменной.",
        "inputs": ["flow"], "output": ["flow"],
        "branches": [{"id": "body", "label": "тело"}, {"id": "done", "label": "дальше"}],
        "defaults": {"count": "3", "var": "i"},
        "fields": [F(key="count", label="Сколько раз", kind="num"),
                   F(key="var", label="Переменная номера", kind="text", optional=True)],
    },
    "while": {
        "title": "Пока", "group": "Логика", "color": "#10b981", "icon": "⟳",
        "description": "Повторять «тело», пока выражение истинно; затем идти «дальше».",
        "inputs": ["flow"], "output": ["flow"],
        "branches": [{"id": "body", "label": "тело"}, {"id": "done", "label": "дальше"}],
        "defaults": {"expr": "n < 10", "max_iterations": "1000", "var": ""},
        "fields": [F(key="expr", label="Условие", kind="expr"),
                   F(key="max_iterations", label="Предел повторов (защита)", kind="num", optional=True),
                   F(key="var", label="Переменная номера", kind="text", optional=True)],
    },
    "condition": {
        "title": "Условие", "group": "Логика", "color": "#ec4899", "icon": "◇",
        "description": "Ветвление: «да», если выражение истинно, иначе «нет».",
        "inputs": ["flow"], "output": ["flow"],
        "branches": [{"id": "true", "label": "да"}, {"id": "false", "label": "нет"}],
        "defaults": {"expr": "n > 5"},
        "fields": [F(key="expr", label="Выражение", kind="expr")],
    },
    "set_vars": {
        "title": "Переменные", "group": "Логика", "color": "#10b981", "icon": "=",
        "description": "Задать или изменить переменные: n = n + 1.",
        "inputs": ["flow"], "output": ["flow"],
        "defaults": {"assignments": [{"target": "n", "expr": "0"}]},
        "fields": [F(key="assignments", label="Присваивания", kind="assignments")],
    },
    "call_scenario": {
        "title": "Выполнить сценарий", "group": "Логика", "color": "#0ea5e9", "icon": "↪",
        "description": "Подпрограмма: выполнить другой сценарий проекта с параметрами и взять его переменные.",
        "inputs": ["flow"], "output": ["flow"],
        "defaults": {"scenario": "", "params": [], "target": "result"},
        "fields": [F(key="scenario", label="Сценарий", kind="scenario",
                     hint="Имя другого сценария этого проекта (его «Старт»)"),
                   F(key="params", label="Параметры", kind="kv",
                     hint="Входные параметры вызываемого «Старта»: имя → выражение"),
                   F(key="target", label="Результат — в переменную", kind="text", optional=True,
                     hint="Переменные сценария после выполнения: result['total']")],
    },
    "log": {
        "title": "Сообщение в лог", "group": "Логика", "color": "#64748b", "icon": "✎",
        "description": "Записать строку в лог запуска: «Шаг {i} из {n}».",
        "inputs": ["flow"], "output": ["flow"],
        "defaults": {"message": "Готово: {i}"},
        "fields": [F(key="message", label="Сообщение", kind="template")],
    },
    "stop": {
        "title": "Завершить", "group": "Логика", "color": "#ef4444", "icon": "■",
        "description": "Закончить сценарий с результатом «успех» или «ошибка».",
        "inputs": ["flow"], "output": [],
        "defaults": {"status": "ok", "message": ""},
        "fields": [F(key="status", label="Результат", kind="select", options=[["ok", "Успех"], ["error", "Ошибка"]]),
                   F(key="message", label="Сообщение", kind="template", optional=True)],
    },
    "telegram": {
        "title": "Telegram: сообщение", "group": "Telegram", "color": "#229ED9", "icon": "✈",
        "description": "Отправить сообщение через бота проекта (настройки → Telegram). {выражения} подставляются.",
        "inputs": ["flow"], "output": ["flow"],
        "defaults": {"text": "Сценарий дошёл до шага {i}", "chat": "", "silent": False},
        "fields": [F(key="text", label="Текст", kind="template", multiline=True),
                   F(key="chat", label="ID чата", kind="text", optional=True,
                     hint="Пусто — во все разрешённые чаты из настроек"),
                   F(key="silent", label="Без звука", kind="bool")],
    },
    "tg_ask": {
        "title": "Спросить в Telegram", "group": "Telegram", "color": "#229ED9", "icon": "❓",
        "description": "Сообщение с кнопками; сценарий ждёт ответа и идёт по ветке «да» или «нет» (и по таймауту).",
        "inputs": ["flow"], "output": ["flow"],
        "branches": [{"id": "true", "label": "да"}, {"id": "false", "label": "нет"}],
        "defaults": {"text": "Продолжить?", "yes": "Да", "no": "Нет", "timeout": "300", "chat": "", "target": "answer"},
        "fields": [F(key="text", label="Вопрос", kind="template", multiline=True),
                   F(key="yes", label="Кнопка «да»", kind="text"),
                   F(key="no", label="Кнопка «нет»", kind="text"),
                   F(key="timeout", label="Ждать ответа, с", kind="num",
                     hint="Нет ответа — ветка «нет». Не дольше лимита времени запуска."),
                   F(key="chat", label="ID чата", kind="text", optional=True,
                     hint="Пусто — во все разрешённые чаты; засчитывается первый ответ"),
                   F(key="target", label="Ответ — в переменную", kind="text", optional=True,
                     hint="True / False, или None при таймауте")],
    },
    "image_find": {
        "title": "Найти картинку", "group": "Зрение", "color": "#c026d3", "icon": "🔍",
        "description": "Найти образец на экране (кнопку, иконку): ждать, пока появится или исчезнет, и кликнуть "
                       "по найденному. «да» — нашли (исчезла), «нет» — не дождались.",
        "inputs": ["flow"], "output": ["flow"],
        "branches": [{"id": "true", "label": "да"}, {"id": "false", "label": "нет"}],
        "defaults": {"image": "", "region": None, "mode": "appear", "wait": "5", "confidence": "0.85",
                     "grayscale": False, "action": "click", "dx": "0", "dy": "0", "duration": "0.2", "target": "found"},
        "fields": [F(key="image", label="Образец", kind="image",
                     hint="Снимок экрана → выделите мышкой то, что искать. Или загрузите PNG."),
                   F(key="mode", label="Ждать, пока картинка", kind="select",
                     options=[["appear", "появится"], ["disappear", "исчезнет"]]),
                   F(key="wait", label="Ждать до, с", kind="num", optional=True,
                     hint="0 — проверить один раз; проверка ~4 раза в секунду"),
                   _win("Переменная из «Найти окно»: искать только в этом окне, область — относительно него"),
                   F(key="region", label="Где искать", kind="region", optional=True,
                     hint="Пусто — весь экран (или всё окно). Область меньше — поиск быстрее и без ложных совпадений"),
                   F(key="confidence", label="Точность совпадения (0.5–1)", kind="num", optional=True,
                     hint="0.85 — обычно; ниже — находит похожее, выше — только точную копию"),
                   F(key="grayscale", label="Без учёта цвета", kind="bool"),
                   F(key="action", label="Когда нашли", kind="select", show_if={"mode": ["appear"]},
                     options=[["none", "Ничего — только ветка «да»"], ["click", "Клик по центру"],
                              ["double", "Двойной клик"], ["right", "Правый клик"], ["move", "Навести курсор"]]),
                   F(key="dx", label="Смещение X", kind="num", optional=True, group="point",
                     show_if={"mode": ["appear"], "action": ["click", "double", "right", "move"]},
                     hint="От центра найденного: например, поле ввода справа от надписи"),
                   F(key="dy", label="Смещение Y", kind="num", optional=True, group="point",
                     show_if={"mode": ["appear"], "action": ["click", "double", "right", "move"]}),
                   F(key="target", label="Результат — в переменную", kind="text", optional=True,
                     hint="found['found'] — нашли ли; found['x'], found['y'] — центр; ещё left, top, w, h, score")],
    },
    "pixel_check": {
        "title": "Цвет пикселя", "group": "Зрение", "color": "#c026d3", "icon": "🎨",
        "description": "Проверить цвет точки экрана (индикатор, полоска здоровья, кнопка стала активной). "
                       "Можно ждать, пока цвет станет нужным.",
        "inputs": ["flow"], "output": ["flow"],
        "branches": [{"id": "true", "label": "да"}, {"id": "false", "label": "нет"}],
        "defaults": {"x": "500", "y": "300", "color": "#00ff00", "tolerance": "10", "wait": "0", "target": "pixel"},
        "fields": [F(key="x", label="X", kind="num", group="point", hint=""),
                   F(key="y", label="Y", kind="num", group="point"),
                   _win(),
                   F(key="color", label="Ожидаемый цвет (#RRGGBB)", kind="color"),
                   F(key="tolerance", label="Допуск (0–255)", kind="num", optional=True,
                     hint="Насколько может отличаться каждый канал: 0 — точно этот цвет"),
                   F(key="wait", label="Ждать до, с", kind="num", optional=True,
                     hint="0 — проверить один раз"),
                   F(key="target", label="Цвет — в переменную", kind="text", optional=True,
                     hint="pixel['hex'] — цвет, pixel['r'], ['g'], ['b'], pixel['match'] — совпал ли")],
        "required": ["x", "y"],
    },
    "ocr": {
        "title": "Прочитать текст", "group": "Зрение", "color": "#c026d3", "icon": "🔤",
        "description": "Распознать текст в области экрана (OCR) и положить в переменную — дальше «Условие»: "
                       "contains(text, 'Готово'), int(text) > 100.",
        "inputs": ["flow"], "output": ["flow"],
        "defaults": {"region": None, "lang": "rus+eng", "digits": False, "target": "text", "window": ""},
        "fields": [_win("Переменная из «Найти окно»: область — относительно окна; без области — всё окно"),
                   F(key="region", label="Область", kind="region", optional=True,
                     hint="Выделите на снимке экрана только нужный текст — так точнее и быстрее"),
                   F(key="lang", label="Язык", kind="select",
                     options=[["rus+eng", "Русский и английский"], ["rus", "Русский"], ["eng", "Английский"]]),
                   F(key="digits", label="Только цифры (счётчики, цены)", kind="bool"),
                   F(key="target", label="Текст — в переменную", kind="text")],
    },
    "window_find": {
        "title": "Найти окно", "group": "Окна", "color": "#ea580c", "icon": "🪟",
        "description": "Найти окно по заголовку или программе: ждать, пока откроется (или закроется), и сделать "
                       "активным. Окно — в переменную: дальше клики «внутри окна» и «Окно: действие».",
        "inputs": ["flow"], "output": ["flow"],
        "branches": [{"id": "true", "label": "да"}, {"id": "false", "label": "нет"}],
        "defaults": {"title": "Блокнот", "match": "contains", "process": "", "mode": "appear", "wait": "5",
                     "activate": True, "target": "win"},
        "fields": [F(key="title", label="Заголовок окна", kind="window_title",
                     hint="Можно часть заголовка; {выражения} подставляются (в регулярном выражении фигурные "
                          "скобки — {{ и }}). Пусто — любое окно программы"),
                   F(key="match", label="Как сравнивать", kind="select",
                     options=[["contains", "Содержит (без учёта регистра)"], ["exact", "Точно совпадает"],
                              ["regex", "Регулярное выражение"]]),
                   F(key="process", label="Программа (имя процесса)", kind="text", optional=True,
                     hint="Например firefox, chrome.exe, code — если у разных программ похожие заголовки"),
                   F(key="mode", label="Ждать, пока окно", kind="select",
                     options=[["appear", "откроется (есть)"], ["disappear", "закроется (нет)"]]),
                   F(key="wait", label="Ждать до, с", kind="num", optional=True, hint="0 — проверить один раз"),
                   F(key="activate", label="Сделать активным (на передний план)", kind="bool",
                     show_if={"mode": ["appear"]}),
                   F(key="target", label="Окно — в переменную", kind="text",
                     hint="win['title'], win['x'], win['y'], win['w'], win['h'], win['state'], win['process']")],
    },
    "window_action": {
        "title": "Окно: действие", "group": "Окна", "color": "#ea580c", "icon": "🗗",
        "description": "Активировать, свернуть, развернуть, восстановить, переместить / изменить размер или закрыть "
                       "окно из «Найти окно».",
        "inputs": ["flow"], "output": ["flow"],
        "defaults": {"window": "win", "action": "activate", "x": "100", "y": "100", "w": "", "h": ""},
        "fields": [F(key="window", label="Окно (переменная)", kind="window",
                     hint="Переменная из блока «Найти окно»"),
                   F(key="action", label="Действие", kind="select",
                     options=[["activate", "Активировать"], ["minimize", "Свернуть"], ["maximize", "Развернуть"],
                              ["restore", "Восстановить"], ["move", "Переместить / размер"], ["close", "Закрыть"]]),
                   F(key="x", label="X", kind="num", optional=True, show_if={"action": ["move"]},
                     hint="Левый верхний угол содержимого окна; пусто — не менять"),
                   F(key="y", label="Y", kind="num", optional=True, show_if={"action": ["move"]}),
                   F(key="w", label="Ширина", kind="num", optional=True, show_if={"action": ["move"]},
                     hint="Пусто — не менять размер"),
                   F(key="h", label="Высота", kind="num", optional=True, show_if={"action": ["move"]})],
    },
    "code": {
        "title": "Python-код", "group": "Данные", "color": "#0f766e", "icon": "🐍",
        "description": "run(data, env) в песочнице платформы; data — переменные сценария.",
        "inputs": ["flow"], "output": ["flow"],
        "defaults": {
            "code": ("def run(data, env):\n"
                     "    # data — переменные сценария, env — переменные окружения проекта.\n"
                     "    # Верните dict с новыми значениями (только поля из «Выходных полей»).\n"
                     "    return {\"n\": int(data.get(\"n\", 0)) + 1}\n"),
            "outputs": ["n"], "timeout": 2, "memory_mb": 256,
        },
        "fields": [F(key="code", label="Код", kind="code")],
    },
    "http": {
        "title": "HTTP-запрос", "group": "Данные", "color": "#7c3aed", "icon": "🌐",
        "description": "Запрос к внешнему API с сервера платформы (по правилам firewall проекта).",
        "inputs": ["flow"], "output": ["flow"],
        "defaults": {"method": "GET", "url": "https://example.com/", "body": [], "target": "response",
                     "timeout": "10"},
        "fields": [F(key="method", label="Метод", kind="select",
                     options=[[m, m] for m in ["GET", "POST", "PUT", "PATCH", "DELETE"]]),
                   F(key="url", label="Адрес", kind="template"),
                   F(key="body", label="JSON-тело", kind="kv", show_if={"method": ["POST", "PUT", "PATCH", "DELETE"]}),
                   F(key="target", label="Ответ — в переменную", kind="text"),
                   F(key="timeout", label="Таймаут, с (до 10)", kind="text", optional=True)],
    },
}


# «Если шаг не удался»: у всех блоков, кроме этих (у циклов ошибки — в теле, «Старт» и «Завершить» не падают)
NO_RETRY = {"trigger", "stop", "repeat", "while"}
ON_ERROR_THEN = ["stop", "skip", "branch"]
MAX_RETRIES = 100


def on_error_policy(data: dict, fail) -> dict | None:
    """data["on_error"] -> {retries, delay, then, var} или None (по умолчанию: без повторов, остановить)."""
    raw = data.get("on_error")
    if not isinstance(raw, dict):
        return None
    out = {"then": raw.get("then") or "stop", "var": str(raw.get("var") or "error").strip() or "error"}
    if out["then"] not in ON_ERROR_THEN:
        fail(f"если шаг не удался: неизвестное действие «{out['then']}»")
        return None
    try:
        out["retries"] = int(str(raw.get("retries") or 0).strip())
        if not 0 <= out["retries"] <= MAX_RETRIES:
            raise ValueError
    except ValueError:
        fail(f"повторить: целое число от 0 до {MAX_RETRIES}")
        return None
    try:
        out["delay"] = float(str(raw.get("delay") or 0).strip().replace(",", "."))
        if not 0 <= out["delay"] <= 3600:
            raise ValueError
    except ValueError:
        fail("пауза между попытками: от 0 до 3600 секунд")
        return None
    if msg := _check_ident(out["var"], "переменная с ошибкой"):
        fail(msg)
        return None
    if out["retries"] == 0 and out["then"] == "stop":
        return None
    return out


def can_connect(source_type: str, target_type: str) -> bool:
    src, dst = AUTO_BLOCKS.get(source_type), AUTO_BLOCKS.get(target_type)
    return bool(src and dst and set(src["output"]) & set(dst["inputs"]))


def catalog() -> dict:
    return {"blocks": AUTO_BLOCKS, "trigger_kinds": TRIGGER_KINDS, "weekdays": WEEKDAYS}


# ---------- план ----------

@dataclass
class Trigger:
    node: str
    name: str
    kind: str
    hotkey: list[str] | None = None
    every_seconds: float | None = None
    at: str | None = None
    weekdays: list[str] = field(default_factory=list)
    params: list[str] = field(default_factory=list)
    command: str | None = None

    def as_dict(self) -> dict:
        return {"node": self.node, "name": self.name, "kind": self.kind, "hotkey": self.hotkey,
                "every_seconds": self.every_seconds, "at": self.at, "weekdays": self.weekdays,
                "params": self.params, "command": self.command}


@dataclass
class Scenario:
    trigger: Trigger
    steps: list[dict]
    code: dict[str, dict] = field(default_factory=dict)   # node -> код и лимиты (выполняет сервер)
    http: dict[str, dict] = field(default_factory=dict)   # node -> метод/таймаут (выполняет сервер)
    calls: set[str] = field(default_factory=set)          # сценарии, которые он вызывает напрямую
    subs: dict[str, dict] = field(default_factory=dict)   # все вызываемые (и их вызовы): имя -> {params, steps}

    def plan(self) -> dict:
        """То, что уходит агенту: без кода (его выполняет сервер)."""
        plan = {"name": self.trigger.name, "trigger": self.trigger.node, "steps": self.steps}
        if self.subs:
            plan["subs"] = self.subs
        return plan

    @property
    def needs_new_agent(self) -> bool:
        """План использует повторы, ветку «ошибка» или подпрограммы — старый агент их не поймёт."""
        return bool(self.subs) or _uses(self.steps, lambda st: "on_error" in st or st.get("op") == "call_scenario")


def _uses(steps, pred) -> bool:
    for st in steps or []:
        if pred(st) or any(_uses(st.get(k), pred) for k in ("then", "else", "body", "next", "on_fail")):
            return True
    return False


def iter_plan_steps(steps):
    for st in steps or []:
        yield st
        for k in ("then", "else", "body", "next", "on_fail"):
            yield from iter_plan_steps(st.get(k))


@dataclass
class AutomationSettings:
    agent: str = "local"
    enabled: bool = False
    max_seconds: int = 600
    max_actions: int = 100_000
    failsafe_corner: bool = True

    @classmethod
    def parse(cls, raw: Any, err) -> "AutomationSettings":
        raw = raw if isinstance(raw, dict) else {}
        s = cls(agent=str(raw.get("agent") or "local"), enabled=bool(raw.get("enabled")),
                failsafe_corner=raw.get("failsafe_corner", True) is not False)
        try:
            s.max_seconds = int(raw.get("max_seconds") or 600)
            s.max_actions = int(raw.get("max_actions") or 100_000)
            if not (1 <= s.max_seconds <= 86400 and 1 <= s.max_actions <= 10_000_000):
                raise ValueError
        except (TypeError, ValueError):
            err(None, "лимиты сценария: время 1–86400 с, действий 1–10 000 000")
        return s

    def limits(self) -> dict:
        return {"max_seconds": self.max_seconds, "max_actions": self.max_actions,
                "failsafe_corner": self.failsafe_corner}

    def as_dict(self) -> dict:
        return {"agent": self.agent, "enabled": self.enabled, **self.limits()}


@dataclass
class TelegramSettings:
    token_env: str = ""
    chats: list[str] = field(default_factory=list)
    notify: str = "errors"          # never | errors | always — сообщать о завершении запусков

    @classmethod
    def parse(cls, raw: Any, env_keys: set[str], err) -> "TelegramSettings":
        raw = raw if isinstance(raw, dict) else {}
        s = cls(token_env=str(raw.get("token_env") or "").strip(), notify=raw.get("notify") or "errors")
        if s.token_env and not re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]*", s.token_env):
            err(None, "Telegram: имя переменной с токеном — латиница, цифры и _")
        elif s.token_env and s.token_env not in env_keys:
            err(None, f"Telegram: в переменных проекта нет {s.token_env} — добавьте её (секрет) с токеном бота")
        for c in raw.get("chats") or []:
            c = str(c).strip()
            if not c:
                continue
            if not TG_CHAT_RE.match(c):
                err(None, f"Telegram: ID чата «{c}» — это число (у групп — отрицательное)")
            elif c not in s.chats:
                s.chats.append(c)
        if s.notify not in TG_NOTIFY:
            err(None, "Telegram: уведомления — never, errors или always")
        return s

    @property
    def configured(self) -> bool:
        return bool(self.token_env)


@dataclass
class CompiledAutomation:
    name: str
    scenarios: list[Scenario]
    env: list[EnvVar]
    settings: AutomationSettings
    warnings: list[Issue]
    sandbox: SandboxSettings
    packages: list[str]
    only_binary: bool
    firewall: FirewallSettings
    telegram: TelegramSettings = field(default_factory=TelegramSettings)

    @property
    def env_values(self) -> dict[str, str]:
        return {e.key: e.value for e in self.env}

    @property
    def telegram_token(self) -> str:
        return self.env_values.get(self.telegram.token_env, "") if self.telegram.token_env else ""

    def telegram_commands(self) -> dict[str, Scenario]:
        return {s.trigger.command: s for s in self.scenarios if s.trigger.kind == "telegram" and s.trigger.command}

    def scenario(self, name: str) -> Scenario | None:
        return next((s for s in self.scenarios if s.trigger.name == name), None)


IMAGE_RE = re.compile(r"^data:image/(png|jpeg|jpg|webp|bmp);base64,[A-Za-z0-9+/=]+$")
MAX_IMAGE_CHARS = 2_800_000   # ~2 МБ картинки в base64


def _region(value) -> dict | None | bool:
    """{x, y, w, h} → целые; None — не задана; False — ошибка."""
    if value in (None, "", {}):
        return None
    if not isinstance(value, dict):
        return False
    try:
        r = {k: int(value[k]) for k in ("x", "y", "w", "h")}
    except (KeyError, TypeError, ValueError):
        return False
    if r["w"] < 1 or r["h"] < 1 or max(abs(v) for v in r.values()) > 100_000:   # x, y < 0 — монитор слева
        return False
    return r


def _check_expr(source, what) -> str | None:
    try:
        expr.parse(str(source or ""))
    except expr.ExprError as e:
        return f"{what}: {e}"
    return None


def _check_template(text, what) -> str | None:
    for m in TEMPLATE_RE.finditer(str(text or "")):
        if m.group(1) is not None and (msg := _check_expr(m.group(1), what)):
            return msg
    return None


def compile_automation(graph: dict) -> CompiledAutomation:
    issues: list[Issue] = []

    def err(node_id, msg):
        issues.append(Issue(node_id, msg))

    def warn(node_id, msg):
        issues.append(Issue(node_id, msg, "warning"))

    env = _compile_env(graph.get("env"), err)
    settings = AutomationSettings.parse(graph.get("automation"), err)
    telegram = TelegramSettings.parse(graph.get("telegram"), {e.key for e in env}, err)
    sandbox_settings, sandbox_errors = SandboxSettings.parse(graph.get("sandbox"))
    libraries = graph.get("libraries") or {}
    packages, package_errors = validate_packages(libraries.get("packages"))
    firewall, firewall_errors = FirewallSettings.parse(graph.get("firewall"), legacy_network=sandbox_settings.network)
    for message in sandbox_errors + package_errors + firewall_errors:
        err(None, message)

    nodes = {n["id"]: n for n in graph.get("nodes", []) if isinstance(n, dict) and "id" in n}
    for nid, n in nodes.items():
        if n.get("type") not in AUTO_BLOCKS:
            err(nid, f"неизвестный тип блока «{n.get('type')}»")

    outgoing: dict[tuple[str, str | None], str] = {}
    for e in graph.get("edges", []):
        s, t = e.get("source"), e.get("target")
        if s not in nodes or t not in nodes:
            err(None, f"связь {e.get('id')} ссылается на несуществующий блок")
            continue
        st, tt = nodes[s].get("type"), nodes[t].get("type")
        if not can_connect(st, tt):
            err(t, f"нельзя соединить «{AUTO_BLOCKS.get(st, {}).get('title', st)}» → "
                   f"«{AUTO_BLOCKS.get(tt, {}).get('title', tt)}»")
            continue
        branches = [b["id"] for b in AUTO_BLOCKS.get(st, {}).get("branches", [])]
        handle = e.get("sourceHandle") if branches else None
        if e.get("sourceHandle") == "error":
            handle = "error"
            if ((nodes[s].get("data") or {}).get("on_error") or {}).get("then") != "branch":
                err(s, "выход «ошибка» работает, только если в «Если шаг не удался» выбрано «перейти по ветке «ошибка»»")
                continue
        elif branches and handle not in branches:
            err(s, f"связь из неизвестного выхода «{handle}»")
            continue
        if (s, handle) in outgoing:
            err(s, "из одного выхода может идти только одна связь")
            continue
        outgoing[(s, handle)] = t

    reached: set[str] = set()
    scenarios: list[Scenario] = []
    names_seen: dict[str, str] = {}
    hotkeys_seen: dict[str, str] = {}
    commands_seen: dict[str, str] = {}
    for nid, n in nodes.items():
        if n.get("type") != "trigger":
            continue
        trig = _trigger(nid, n.get("data") or {}, err)
        if trig is not None and trig.name in names_seen:
            err(nid, f"сценарий с именем «{trig.name}» уже есть")
            trig = None
        if trig is not None:
            names_seen[trig.name] = nid
            if trig.command:
                if trig.command in commands_seen:
                    err(nid, f"команда /{trig.command} уже занята другим сценарием")
                commands_seen[trig.command] = nid
                if not telegram.configured:
                    err(nid, "запуск из Telegram: укажите бота в «Настройки проекта → Telegram»")
                elif not telegram.chats:
                    warn(nid, "в настройках Telegram нет разрешённых чатов — команды никто не сможет отправить")
            if trig.hotkey:
                combo = "+".join(trig.hotkey)
                if combo in hotkeys_seen:
                    err(nid, f"сочетание {combo} уже занято другим сценарием")
                hotkeys_seen[combo] = nid
        # цепочку проверяем, даже если сам «Старт» с ошибкой, — чтобы показать все ошибки сразу
        walker = _Walker(nodes, outgoing, reached, err, warn, telegram)
        reached.add(nid)
        steps = walker.walk(outgoing.get((nid, None)), frozenset({nid}), None)
        if steps is None or trig is None or not walker.ok:
            continue
        if not steps:
            warn(nid, "к блоку «Старт» ничего не подключено — сценарий пустой")
        scenarios.append(Scenario(trig, steps, walker.code, walker.http, walker.calls))

    _link_calls(scenarios, nodes, names_seen, err, warn)

    errors, seen = [], set()
    for i in issues:
        if i.level == "error" and (i.node_id, i.message) not in seen:
            seen.add((i.node_id, i.message))
            errors.append(i)
    if errors:
        raise GraphError(errors)
    warnings = [i for i in issues if i.level == "warning"]
    warnings += [Issue(nid, "блок не подключён ни к одному «Старту» — он не будет выполняться", "warning")
                 for nid in nodes if nid not in reached]
    if not any(n.get("type") == "trigger" for n in nodes.values()):
        warnings.append(Issue(None, "в проекте нет ни одного блока «Старт»", "warning"))
    return CompiledAutomation(str(graph.get("name") or ""), scenarios, env, settings, warnings,
                              sandbox_settings, packages, bool(libraries.get("only_binary", True)), firewall,
                              telegram)


MAX_CALL_DEPTH = 20


def _link_calls(scenarios: list[Scenario], nodes: dict, names_seen: dict, err, warn):
    """Подпрограммы: проверить вызовы, запретить рекурсию и приложить к каждому плану вызываемые сценарии
    (вместе с их Python-кодом и HTTP-блоками — их выполняет сервер)."""
    by_name = {s.trigger.name: s for s in scenarios}
    for sc in scenarios:
        for st in iter_plan_steps(sc.steps):
            if st.get("op") != "call_scenario":
                continue
            callee = by_name.get(st["scenario"])
            if callee is None:
                err(st["node"], f"сценария «{st['scenario']}» нет" + (" — в нём ошибки" if st["scenario"] in names_seen else ""))
                continue
            declared = set(callee.trigger.params)
            given = {p["key"] for p in st.get("params") or []}
            for extra in sorted(given - declared):
                err(st["node"], f"у сценария «{callee.trigger.name}» нет параметра «{extra}» — объявите его в его «Старте»")
            for missing in sorted(declared - given):
                warn(st["node"], f"параметр «{missing}» сценария «{callee.trigger.name}» не передан — будет пустым")

    def chain(name, stack):
        """Все сценарии, вызываемые из name (транзитивно); цикл — ошибка."""
        found = set()
        for callee in sorted(by_name[name].calls):
            if callee not in by_name:
                continue
            if callee in stack:
                loop = " → ".join([*stack[stack.index(callee):], callee])
                node = next((st["node"] for st in iter_plan_steps(by_name[name].steps)
                             if st.get("op") == "call_scenario" and st.get("scenario") == callee), None)
                err(node, f"сценарии вызывают друг друга по кругу: {loop}")
                continue
            if len(stack) >= MAX_CALL_DEPTH:
                err(None, f"слишком глубокая вложенность подпрограмм (больше {MAX_CALL_DEPTH})")
                continue
            found |= {callee} | chain(callee, stack + [callee])
        return found

    for sc in scenarios:
        for name in sorted(chain(sc.trigger.name, [sc.trigger.name])):
            callee = by_name[name]
            sc.subs[name] = {"params": list(callee.trigger.params), "steps": callee.steps}
            sc.code.update(callee.code)
            sc.http.update(callee.http)


def _trigger(nid: str, data: dict, err) -> Trigger | None:
    ok = True

    def fail(msg):
        nonlocal ok
        ok = False
        err(nid, msg)

    name = str(data.get("name") or "").strip()
    if not NAME_RE.match(name):
        fail("имя сценария: латиница, цифры, - и _ (до 40 символов)")
    kind = data.get("kind") or "manual"
    if kind not in TRIGGER_KINDS:
        fail(f"неизвестный способ запуска «{kind}»")
    t = Trigger(nid, name, kind)
    if kind == "hotkey":
        try:
            t.hotkey = parse_combo(data.get("hotkey"))
            if not any(k in ("ctrl", "alt", "super") for k in t.hotkey) and not (
                    len(t.hotkey) == 1 and re.fullmatch(r"f\d+|pause|scroll_lock", t.hotkey[0])):
                fail("горячая клавиша должна включать Ctrl, Alt или Super (или быть F1–F20, Pause) — "
                     "иначе она будет срабатывать при обычном наборе текста")
        except KeyError_ as e:
            fail(f"горячая клавиша: {e}")
    if kind == "interval":
        try:
            t.every_seconds = float(str(data.get("every_seconds")).strip())
            if not 5 <= t.every_seconds <= 7 * 86400:
                raise ValueError
        except (TypeError, ValueError):
            fail("интервал: число секунд от 5 до 604800")
    if kind == "daily":
        t.at = str(data.get("at") or "").strip()
        if not TIME_RE.match(t.at):
            fail("время запуска: формат ЧЧ:ММ, например 09:30")
        t.weekdays = [d for d in data.get("weekdays") or [] if d in WEEKDAYS]
    if kind == "telegram":
        t.command = str(data.get("command") or "").strip().lstrip("/").lower()
        if not TG_COMMAND_RE.match(t.command):
            fail("команда бота: латиница в нижнем регистре, цифры и _ (до 32 символов)")
        elif t.command in TG_BUILTIN:
            fail(f"/{t.command} — встроенная команда бота, выберите другое имя")
    for p in data.get("params") or []:
        if msg := _check_ident(p, "входной параметр"):
            fail(msg)
        elif p in t.params:
            fail(f"входной параметр «{p}» объявлен дважды")
        else:
            t.params.append(p)
    return t if ok else None


class _Walker:
    def __init__(self, nodes, outgoing, reached, err, warn, telegram=None):
        self.telegram = telegram or TelegramSettings()
        self.blocks = AUTO_BLOCKS
        self.nodes, self.outgoing, self.reached = nodes, outgoing, reached
        self.err, self.warn = err, warn
        self.budget = MAX_STEPS
        self.ok = True
        self.code: dict[str, dict] = {}
        self.http: dict[str, dict] = {}
        self.calls: set[str] = set()

    def fail(self, nid, msg):
        self.ok = False
        self.err(nid, msg)

    def walk(self, cur: str | None, path: frozenset, loop: str | None) -> list[dict] | None:
        """Шаги от cur до конца цепочки. loop — блок цикла, в тело которого мы зашли:
        связь обратно в него означает «конец тела»."""
        steps: list[dict] = []
        path = set(path)
        while cur is not None:
            if cur == loop:
                break
            if cur in path:
                self.fail(cur, "цикл в цепочке: для повторов используйте блоки «Повторить» или «Пока»")
                return None
            self.budget -= 1
            if self.budget < 0:
                self.fail(cur, f"сценарий слишком разветвлён (больше {MAX_STEPS} шагов во всех ветках)")
                return None
            path.add(cur)
            self.reached.add(cur)
            node = self.nodes[cur]
            ntype, data = node.get("type"), node.get("data") or {}
            if ntype == "trigger":
                self.fail(cur, "«Старт» может быть только началом сценария")
                return None
            policy = self._policy(cur, ntype, data)
            on_fail = None
            if policy and policy["then"] == "branch":
                start = self.outgoing.get((cur, "error"))
                if start is None:
                    self.warn(cur, "ветка «ошибка» не подключена — при ошибке сценарий остановится")
                else:
                    on_fail = self.walk(start, frozenset(path), loop)
                    if on_fail is None:
                        return None
            if ntype in ("condition", "tg_ask", "image_find", "pixel_check", "window_find"):
                step = self._step(cur, ntype, data)
                then = self.walk(self.outgoing.get((cur, "true")), frozenset(path), loop)
                other = self.walk(self.outgoing.get((cur, "false")), frozenset(path), loop)
                if step is not None and then is not None and other is not None:
                    op = "if" if ntype == "condition" else ntype
                    steps.append(self._with_policy({**step, "op": op, "then": then, "else": other}, policy, on_fail))
                return steps   # продолжение — внутри веток
            if ntype in ("repeat", "while"):
                step = self._step(cur, ntype, data)
                body_start = self.outgoing.get((cur, "body"))
                if body_start is None:
                    self.warn(cur, "у цикла не подключено «тело» — он ничего не делает")
                body = self.walk(body_start, frozenset(path), cur)
                if step is not None and body is not None:
                    steps.append({**step, "body": body})
                cur = self.outgoing.get((cur, "done"))
                continue
            step = self._step(cur, ntype, data)
            if on_fail is not None:
                # шаг с веткой «ошибка» — развилка: продолжение после успеха (next) или ветка «ошибка»
                nxt = self.walk(self.outgoing.get((cur, None)), frozenset(path), loop)
                if step is not None and nxt is not None:
                    steps.append(self._with_policy({**step, "next": nxt}, policy, on_fail))
                return steps if nxt is not None else None
            if step is not None:   # с ошибкой — идём дальше, чтобы показать ошибки всех блоков сразу
                steps.append(self._with_policy(step, policy, None))
            if ntype == "stop":
                return steps
            cur = self.outgoing.get((cur, None))
        return steps

    def _policy(self, nid, ntype, data):
        if ntype in NO_RETRY:
            return None
        return on_error_policy(data, lambda m: self.fail(nid, m))

    @staticmethod
    def _with_policy(step, policy, on_fail):
        if policy:
            step["on_error"] = policy
        if on_fail is not None:
            step["on_fail"] = on_fail
        return step

    def _step(self, nid: str, ntype: str, data: dict) -> dict | None:
        spec = self.blocks[ntype]
        ok = True

        def fail(msg):
            nonlocal ok
            ok = False
            self.fail(nid, msg)

        step: dict[str, Any] = {"op": ntype, "node": nid}
        for f in spec.get("fields", []):
            key, kind = f["key"], f["kind"]
            if f.get("show_if") and not all(data.get(k, spec["defaults"].get(k)) in v
                                            for k, v in f["show_if"].items()):
                continue
            value = data.get(key, spec["defaults"].get(key))
            label = f["label"]
            empty = value is None or (isinstance(value, str) and not value.strip())
            required = not f.get("optional") or key in spec.get("required", [])
            if kind in ("num", "expr"):
                if empty:
                    if required:
                        fail(f"«{label}»: не задано")
                    step[key] = ""
                    continue
                if msg := _check_expr(value, label):
                    fail(msg)
                step[key] = str(value).strip()
            elif kind == "template":
                if empty and required:
                    fail(f"«{label}»: пусто")
                if msg := _check_template(value, label):
                    fail(msg)
                step[key] = str(value or "")
            elif kind == "select":
                allowed = [o[0] for o in f["options"]]
                if value not in allowed:
                    fail(f"«{label}»: недопустимое значение «{value}»")
                step[key] = value
            elif kind == "bool":
                step[key] = bool(value)
            elif kind == "keys":
                try:
                    step[key] = parse_combo(value)
                except KeyError_ as e:
                    fail(f"«{label}»: {e}")
            elif kind == "text":
                value = str(value or "").strip()
                if key in ("target", "var") and value and (msg := _check_ident(value, label)):
                    fail(msg)
                elif required and not value:
                    fail(f"«{label}»: пусто")
                step[key] = value
            elif kind == "assignments":
                items = []
                for a in value or []:
                    target = str(a.get("target") or "").strip()
                    if msg := _check_ident(target, "переменная"):
                        fail(msg)
                        continue
                    if msg := _check_expr(a.get("expr"), f"«{target}»"):
                        fail(msg)
                        continue
                    items.append({"target": target, "expr": str(a["expr"]).strip()})
                if not items and ok:
                    fail("нет ни одного присваивания")
                step[key] = items
            elif kind in ("window", "window_title"):
                text = str(value or "").strip()
                if kind == "window" and text and (msg := _check_ident(text, label)):
                    fail(msg)
                elif kind == "window" and required and not text:
                    fail(f"«{label}»: укажите переменную из «Найти окно»")
                elif kind == "window_title" and (msg := _check_template(text, label)):
                    fail(msg)
                step[key] = text
            elif kind == "image":
                text = str(value or "").strip()
                if not text:
                    fail("нет образца: «📷 Снимок экрана» → выделите, что искать, или загрузите PNG")
                elif not IMAGE_RE.match(text):
                    fail("образец: нужна картинка PNG или JPEG")
                elif len(text) > MAX_IMAGE_CHARS:
                    fail("образец больше 2 МБ — выделите область поменьше")
                step[key] = text
            elif kind == "region":
                region = _region(value)
                if region is False:
                    fail(f"«{label}»: x, y, ширина и высота — целые числа, ширина и высота больше 0")
                elif region is None and required:
                    fail(f"«{label}»: выделите область на снимке экрана")
                step[key] = region or None
            elif kind == "color":
                text = str(value or "").strip()
                if not re.fullmatch(r"#?[0-9a-fA-F]{6}", text):
                    fail(f"«{label}»: цвет в формате #RRGGBB, например #22c55e")
                step[key] = "#" + text.lstrip("#").lower()
            elif kind == "scenario":
                value = str(value or "").strip()
                if not value:
                    fail("«Сценарий»: выберите, какой сценарий выполнить")
                step[key] = value
                self.calls.add(value)
            elif kind == "kv":
                items = []
                for a in value or []:
                    k = str(a.get("key") or "").strip()
                    if not k:
                        fail("пустое имя поля в теле запроса")
                        continue
                    if msg := _check_expr(a.get("expr"), f"поле «{k}»"):
                        fail(msg)
                        continue
                    items.append({"key": k, "expr": str(a["expr"]).strip()})
                step[key] = items

        if ntype == "ocr" and not step.get("region") and not step.get("window"):
            fail("«Область»: выделите область на снимке экрана (или укажите окно)")
        if ntype == "window_find" and not step.get("title") and not str(data.get("process") or "").strip():
            fail("укажите заголовок окна или программу")
        if ntype == "window_find" and step.get("match") == "regex" and "{" not in str(step.get("title")):
            try:
                re.compile(step.get("title") or "")
            except re.error as e:
                fail(f"регулярное выражение заголовка: {e}")
        if ntype in ("mouse_click", "mouse_down", "mouse_up", "mouse_scroll", "autoclick", "mouse_drag"):
            pairs = [("x1", "y1")] if ntype == "mouse_drag" else [("x", "y")]
            for xk, yk in pairs:
                if bool(step.get(xk)) != bool(step.get(yk)):
                    fail("задайте обе координаты (X и Y) или ни одной")
        if ntype == "code":
            code_step, errors, warnings = check_code_block(nid, data)
            for m in errors:
                fail(m)
            for m in warnings:
                self.warn(nid, m)
            if code_step is not None:
                step = {"op": "code", "node": nid, "outputs": code_step.outputs}
                self.code[nid] = {"code": code_step.code, "outputs": code_step.outputs,
                                  "timeout": code_step.timeout, "memory_mb": code_step.memory_mb}
        if ntype in ("telegram", "tg_ask"):
            if not self.telegram.configured:
                fail("бот не настроен: «Настройки проекта → Telegram»")
            chat = step.get("chat") or ""
            if chat and not TG_CHAT_RE.match(chat):
                fail("ID чата — число (у групп — отрицательное)")
            elif chat and self.telegram.chats and chat not in self.telegram.chats:
                fail(f"чат {chat} не в списке разрешённых (настройки → Telegram)")
            elif not chat and not self.telegram.chats:
                fail("нет разрешённых чатов: добавьте их в «Настройки проекта → Telegram» или укажите ID чата")
            if ntype == "tg_ask" and (not str(step.get("yes", "")).strip() or not str(step.get("no", "")).strip()):
                fail("подписи кнопок не должны быть пустыми")
        if ntype == "http":
            try:
                timeout = float(str(data.get("timeout") or 10))
                if not 0.5 <= timeout <= 10:
                    raise ValueError
            except ValueError:
                fail("таймаут: от 0.5 до 10 секунд")
                timeout = 10
            self.http[nid] = {"method": step.get("method", "GET"), "timeout": timeout}
            step.pop("timeout", None)
        return step if ok else None


# ---------- запись действий → блоки ----------

PRINTABLE_EXCLUDE = {"enter", "tab", "esc", "backspace", "delete"}


def recording_to_blocks(events: list[dict], *, min_wait: float = 0.4, keep_waits: bool = True,
                        drop_last_click: bool = False) -> list[dict]:
    """Сырые события агента → цепочка блоков [{type, data}].

    События: {"t": сек, "kind": "down"|"up", "button", "x", "y"} (мышь), {"t", "kind": "scroll",
    "dx", "dy", "x", "y"}, {"t", "kind": "key_down"|"key_up", "key"} (key — нормализованное имя
    или символ). Последовательные символы склеиваются в «Ввести текст», сочетания с Ctrl/Alt/Super —
    в «Нажать клавиши», нажатие+отпускание кнопки мыши — в клик (двойной — clicks=2) или
    перетаскивание; паузы длиннее min_wait — в блок «Пауза».
    """
    events = sorted((e for e in events if isinstance(e, dict) and isinstance(e.get("t"), (int, float))),
                    key=lambda e: e["t"])
    if drop_last_click:  # щелчок по кнопке «Остановить запись» в браузере
        downs = [i for i, e in enumerate(events) if e.get("kind") == "down"]
        if downs:
            events = events[:downs[-1]]

    actions: list[tuple[float, float, dict]] = []   # (начало, конец, блок)
    held_mods: list[str] = []
    text_buf: list[str] = []
    text_start = text_end = 0.0
    pressed: dict[str, dict] = {}

    def flush_text():
        nonlocal text_buf
        if text_buf:
            actions.append((text_start, text_end, {"type": "key_type", "data": {
                "text": "".join(text_buf).replace("{", "{{").replace("}", "}}"), "interval_ms": "20",
                "enter": False}}))
            text_buf = []

    for e in events:
        kind, t = e.get("kind"), float(e["t"])
        if kind == "key_down":
            key = e.get("key")
            if not key:
                continue
            if key in ("ctrl", "alt", "super", "shift"):
                if key not in held_mods:
                    held_mods.append(key)
                continue
            combo_mods = [m for m in held_mods if m != "shift"]
            if combo_mods:
                flush_text()
                k = key.lower() if len(key) == 1 else key
                mods = [m for m in ["ctrl", "shift", "alt", "super"] if m in held_mods]
                actions.append((t, t, {"type": "key_press", "data": {"keys": "+".join(mods + [k]),
                                                                     "action": "tap", "times": "1"}}))
            elif len(key) == 1 or key == "space":
                if not text_buf:
                    text_start = t
                text_buf.append(" " if key == "space" else key)
                text_end = t
            else:
                flush_text()
                prev = actions[-1][2] if actions else None
                if (prev and prev["type"] == "key_press" and prev["data"]["keys"] == key
                        and t - actions[-1][1] < 0.6):
                    prev["data"]["times"] = str(int(prev["data"]["times"]) + 1)
                    actions[-1] = (actions[-1][0], t, prev)
                else:
                    mods = ["shift"] if "shift" in held_mods else []
                    actions.append((t, t, {"type": "key_press", "data": {"keys": "+".join(mods + [key]),
                                                                         "action": "tap", "times": "1"}}))
        elif kind == "key_up":
            key = e.get("key")
            if key in held_mods:
                held_mods.remove(key)
        elif kind == "down":
            flush_text()
            pressed[e.get("button", "left")] = e
        elif kind == "up":
            down = pressed.pop(e.get("button", "left"), None)
            if down is None:
                continue
            button = e.get("button", "left")
            dist = abs(e["x"] - down["x"]) + abs(e["y"] - down["y"])
            if dist > 6:
                actions.append((float(down["t"]), t, {"type": "mouse_drag", "data": {
                    "x1": str(down["x"]), "y1": str(down["y"]), "x2": str(e["x"]), "y2": str(e["y"]),
                    "button": button, "duration": str(round(min(max(t - float(down["t"]), 0.1), 5), 2))}}))
                continue
            prev = actions[-1] if actions else None
            if (prev and prev[2]["type"] == "mouse_click" and prev[2]["data"]["button"] == button
                    and float(down["t"]) - prev[1] < 0.4 and int(prev[2]["data"]["clicks"]) < 3
                    and abs(int(prev[2]["data"]["x"]) - down["x"]) + abs(int(prev[2]["data"]["y"]) - down["y"]) <= 6):
                prev[2]["data"]["clicks"] = str(int(prev[2]["data"]["clicks"]) + 1)
                actions[-1] = (prev[0], t, prev[2])
                continue
            actions.append((float(down["t"]), t, {"type": "mouse_click", "data": {
                "x": str(down["x"]), "y": str(down["y"]), "button": button, "clicks": "1", "duration": "0"}}))
        elif kind == "scroll":
            flush_text()
            prev = actions[-1] if actions else None
            if (prev and prev[2]["type"] == "mouse_scroll" and t - prev[1] < 0.5
                    and prev[2]["data"]["x"] == str(e.get("x")) and prev[2]["data"]["y"] == str(e.get("y"))):
                d = prev[2]["data"]
                d["dy"] = str(int(d["dy"]) + int(e.get("dy", 0)))
                d["dx"] = str(int(d["dx"]) + int(e.get("dx", 0)))
                actions[-1] = (prev[0], t, prev[2])
            else:
                actions.append((t, t, {"type": "mouse_scroll", "data": {
                    "dy": str(int(e.get("dy", 0))), "dx": str(int(e.get("dx", 0))),
                    "x": str(e.get("x")), "y": str(e.get("y"))}}))
    flush_text()

    blocks: list[dict] = []
    last_end = None
    for start, end, block in actions:
        if keep_waits and last_end is not None and start - last_end >= min_wait:
            blocks.append({"type": "wait", "data": {"seconds": str(round(start - last_end, 1))}})
        blocks.append(block)
        last_end = end
    return blocks
