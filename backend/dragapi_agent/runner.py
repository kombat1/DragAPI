"""Исполнитель сценария: проходит по скомпилированному плану и выполняет шаги через драйвер ввода.

План — JSON, который собирает платформа (app/automation.py): список шагов {"op": ..., "node": ...};
у «if» есть then/else, у «repeat»/«while» — body. Выражения и шаблоны вычисляются здесь же
тем же безопасным вычислителем, что и в API-проектах.

Безопасность: остановка по stop_event (кнопка в платформе, горячая клавиша агента, обрыв связи),
аварийный угол экрана (курсор в левом верхнем углу), лимиты на время и число действий.
"""

from __future__ import annotations

import math
import re
import threading
import time
from typing import Any, Callable

try:
    from app.expr import ExprError, evaluate, parse
except ImportError:  # агент, скачанный отдельно, несёт копию expr.py с собой
    from .expr import ExprError, evaluate, parse  # type: ignore

from .keys import MODIFIERS

DEFAULT_LIMITS = {"max_seconds": 600, "max_actions": 100_000, "failsafe_corner": True}
MIN_CLICK_INTERVAL = 0.01
MAX_REPEAT = 1_000_000
TEMPLATE_RE = re.compile(r"\{\{|\}\}|\{([^{}]+)\}")


class Stopped(Exception):
    pass


class StepError(Exception):
    def __init__(self, node, message):
        super().__init__(message)
        self.node = node


class Finish(Exception):
    def __init__(self, status, message):
        super().__init__(message)
        self.status, self.message = status, message


def render_template(text: str, scope: dict, env: dict) -> str:
    """«Привет, {name}! Шаг {i + 1}» — подстановка выражений; {{ и }} — сами скобки."""
    def sub(m):
        if m.group(0) == "{{":
            return "{"
        if m.group(0) == "}}":
            return "}"
        value = evaluate(parse(m.group(1)), scope, env)
        return "" if value is None else str(value)
    return TEMPLATE_RE.sub(sub, str(text or ""))


def jsonable(value: Any, depth: int = 0) -> Any:
    if value is None or isinstance(value, (bool, int, float, str)):
        if isinstance(value, float) and not math.isfinite(value):
            return str(value)
        return value
    if depth > 8:
        return str(value)
    if isinstance(value, dict):
        return {str(k): jsonable(v, depth + 1) for k, v in list(value.items())[:500]}
    if isinstance(value, (list, tuple, set)):
        return [jsonable(v, depth + 1) for v in list(value)[:500]]
    return str(value)


class Runner:
    def __init__(self, scenario: dict, driver, *, env: dict | None = None, inputs: dict | None = None,
                 emit: Callable[[dict], None] | None = None,
                 server_op: Callable[[str, str, dict], dict] | None = None,
                 limits: dict | None = None, stop_event: threading.Event | None = None):
        self.scenario = scenario
        self.d = driver
        self.env = dict(env or {})
        self.vars: dict[str, Any] = dict(inputs or {})
        self.emit = emit or (lambda e: None)
        self.server_op = server_op
        self.limits = {**DEFAULT_LIMITS, **(limits or {})}
        self.stop = stop_event or threading.Event()
        self.actions = 0
        self.started = 0.0
        self.node = None
        self.last_target: tuple | None = None

    # --- служебное ---

    def check(self):
        if self.stop.is_set():
            raise Stopped("остановлено")
        if time.monotonic() - self.started > self.limits["max_seconds"]:
            raise StepError(self.node, f"сценарий идёт дольше {self.limits['max_seconds']} с — остановлен лимитом")

    def act(self, n: int = 1):
        """Перед каждым действием ввода: стоп, лимиты, аварийный угол."""
        self.check()
        self.actions += n
        if self.actions > self.limits["max_actions"]:
            raise StepError(self.node, f"больше {self.limits['max_actions']} действий — остановлено лимитом")
        if self.limits.get("failsafe_corner"):
            pos = self.d.position()
            if pos[0] <= 1 and pos[1] <= 1 and self.last_target != (0, 0):
                raise Stopped("курсор в левом верхнем углу экрана — аварийная остановка")

    def sleep(self, seconds: float):
        end = time.monotonic() + max(0.0, seconds)
        while True:
            self.check()
            left = end - time.monotonic()
            if left <= 0:
                return
            self.stop.wait(min(left, 0.05))

    def ev(self, expr: str, what: str):
        try:
            return evaluate(parse(str(expr)), self.vars, self.env)
        except ExprError as e:
            raise StepError(self.node, f"{what}: {e}") from None

    def text(self, template: str, what: str) -> str:
        try:
            return render_template(template, self.vars, self.env)
        except ExprError as e:
            raise StepError(self.node, f"{what}: {e}") from None

    def num(self, expr, what: str, *, integer=False, minimum=None, maximum=None, default=None):
        if expr is None or str(expr).strip() == "":
            if default is not None:
                return default
            raise StepError(self.node, f"{what}: не задано")
        value = self.ev(expr, what)
        if isinstance(value, bool) or not isinstance(value, (int, float)):
            try:
                value = float(str(value).strip())
            except ValueError:
                raise StepError(self.node, f"{what}: ожидалось число, получено {value!r}") from None
        if not math.isfinite(value):
            raise StepError(self.node, f"{what}: ожидалось конечное число")
        if integer:
            value = int(round(value))
        if minimum is not None and value < minimum:
            raise StepError(self.node, f"{what}: не меньше {minimum}, получено {value}")
        if maximum is not None and value > maximum:
            raise StepError(self.node, f"{what}: не больше {maximum}, получено {value}")
        return value

    def point(self, step, xk="x", yk="y"):
        """Координаты шага или None, если обе пустые (действие в текущей позиции)."""
        x, y = step.get(xk), step.get(yk)
        if (x is None or str(x).strip() == "") and (y is None or str(y).strip() == ""):
            return None
        return (self.num(x, f"координата {xk}", integer=True), self.num(y, f"координата {yk}", integer=True))

    def glide(self, target, duration: float):
        """Плавное перемещение курсора (с замедлением в начале и конце)."""
        self.last_target = tuple(target)
        start = self.d.position()
        steps = max(1, min(int(duration * 60), 600))
        if duration <= 0 or start == tuple(target):
            self.act()
            self.d.move(*target)
            return
        for i in range(1, steps + 1):
            self.act()
            t = i / steps
            k = t * t * (3 - 2 * t)
            self.d.move(round(start[0] + (target[0] - start[0]) * k), round(start[1] + (target[1] - start[1]) * k))
            if i < steps:
                self.sleep(duration / steps)

    # --- исполнение ---

    def run(self) -> dict:
        self.started = time.monotonic()
        status, message = "ok", ""
        try:
            self.block(self.scenario.get("steps") or [])
        except Finish as f:
            status, message = f.status, f.message
        except Stopped as s:
            status, message = "stopped", str(s)
        except StepError as e:
            status, message = "error", str(e)
            self.emit({"type": "step", "node": e.node, "status": "error", "message": str(e)})
        finally:
            self.release_all()
        return {"status": status, "message": message, "vars": jsonable(self.vars), "actions": self.actions,
                "duration": round(time.monotonic() - self.started, 3)}

    def release_all(self):
        """Не оставлять зажатые клавиши и кнопки после остановки или ошибки."""
        for key in list(getattr(self, "_held_keys", [])):
            try:
                self.d.key_up(key)
            except Exception:  # noqa: BLE001
                pass
        for button in list(getattr(self, "_held_buttons", [])):
            try:
                self.d.release(button)
            except Exception:  # noqa: BLE001
                pass

    def block(self, steps: list[dict]):
        for step in steps:
            self.check()
            self.node = step.get("node")
            op = step["op"]
            self.emit({"type": "step", "node": self.node, "op": op, "status": "start"})
            handler = getattr(self, "op_" + op, None)
            if handler is None:
                raise StepError(self.node, f"агент не умеет выполнять «{op}» — обновите агента")
            try:
                handler(step)
            except (Stopped, StepError, Finish):
                raise
            except Exception as e:  # noqa: BLE001 — сбой драйвера ввода и т.п.: показать, в каком блоке
                raise StepError(self.node, f"{type(e).__name__}: {e}") from None
            self.node = step.get("node")

    # мышь

    def op_mouse_move(self, s):
        target = self.point(s)
        if target is None:
            raise StepError(self.node, "не заданы координаты")
        if s.get("relative"):
            cur = self.d.position()
            target = (cur[0] + target[0], cur[1] + target[1])
        self.glide(target, self.num(s.get("duration"), "длительность", minimum=0, maximum=60, default=0))

    def _goto(self, s):
        target = self.point(s)
        if target is not None:
            self.glide(target, self.num(s.get("duration"), "длительность", minimum=0, maximum=60, default=0))

    def op_mouse_click(self, s):
        self._goto(s)
        clicks = self.num(s.get("clicks"), "число щелчков", integer=True, minimum=1, maximum=3, default=1)
        self.act()
        self.d.click(s.get("button") or "left", clicks)

    def op_mouse_down(self, s):
        self._goto(s)
        self.act()
        button = s.get("button") or "left"
        self.d.press(button)
        self.__dict__.setdefault("_held_buttons", set()).add(button)

    def op_mouse_up(self, s):
        self._goto(s)
        self.act()
        button = s.get("button") or "left"
        self.d.release(button)
        self.__dict__.setdefault("_held_buttons", set()).discard(button)

    def op_mouse_drag(self, s):
        start = self.point(s, "x1", "y1")
        end = self.point(s, "x2", "y2")
        if end is None:
            raise StepError(self.node, "не задана точка, куда перетащить")
        if start is not None:
            self.glide(start, 0)
        button = s.get("button") or "left"
        self.act()
        self.d.press(button)
        held = self.__dict__.setdefault("_held_buttons", set())
        held.add(button)
        self.sleep(0.05)
        self.glide(end, self.num(s.get("duration"), "длительность", minimum=0, maximum=60, default=0.3))
        self.sleep(0.05)
        self.act()
        self.d.release(button)
        held.discard(button)

    def op_mouse_scroll(self, s):
        self._goto(s)
        dy = self.num(s.get("dy"), "прокрутка по вертикали", integer=True, minimum=-1000, maximum=1000, default=0)
        dx = self.num(s.get("dx"), "прокрутка по горизонтали", integer=True, minimum=-1000, maximum=1000, default=0)
        for _ in range(max(abs(dx), abs(dy))):  # по одному «щелчку» колеса — так прокрутку видят все программы
            self.act()
            self.d.scroll((dx > 0) - (dx < 0) if dx else 0, (dy > 0) - (dy < 0) if dy else 0)
            dx -= (dx > 0) - (dx < 0)
            dy -= (dy > 0) - (dy < 0)
            self.sleep(0.01)

    def op_autoclick(self, s):
        self._goto(s)
        button = s.get("button") or "left"
        interval = self.num(s.get("interval_ms"), "интервал", minimum=MIN_CLICK_INTERVAL * 1000,
                            maximum=3_600_000, default=100) / 1000
        mode = s.get("mode") or "count"
        count = self.num(s.get("count"), "число кликов", integer=True, minimum=1, maximum=MAX_REPEAT) \
            if mode == "count" else None
        seconds = self.num(s.get("seconds"), "длительность", minimum=0.01, maximum=86400) \
            if mode == "seconds" else None
        done, began = 0, time.monotonic()
        while True:
            if count is not None and done >= count:
                break
            if seconds is not None and time.monotonic() - began >= seconds:
                break
            self.act()
            self.d.click(button, 1)
            done += 1
            self.sleep(interval)
        if s.get("target"):
            self.vars[s["target"]] = done
        self.emit({"type": "log", "node": self.node, "message": f"кликов: {done}"})

    # клавиатура

    def _tap(self, keys: list[str]):
        held = self.__dict__.setdefault("_held_keys", set())
        for k in keys:
            self.act()
            self.d.key_down(k)
            held.add(k)
        for k in reversed(keys):
            self.d.key_up(k)
            held.discard(k)

    def op_key_type(self, s):
        text = self.text(s.get("text"), "текст")
        interval = self.num(s.get("interval_ms"), "интервал", minimum=0, maximum=10_000, default=0) / 1000
        for ch in text:
            self.act()
            if ch == "\n":
                self._tap(["enter"])
            else:
                self.d.type_char(ch)
            if interval:
                self.sleep(interval)
        if s.get("enter"):
            self._tap(["enter"])

    def op_key_press(self, s):
        keys = list(s.get("keys") or [])
        action = s.get("action") or "tap"
        held = self.__dict__.setdefault("_held_keys", set())
        if action == "down":
            for k in keys:
                self.act()
                self.d.key_down(k)
                held.add(k)
            return
        if action == "up":
            for k in reversed(keys):
                self.act()
                self.d.key_up(k)
                held.discard(k)
            return
        times = self.num(s.get("times"), "сколько раз", integer=True, minimum=1, maximum=10_000, default=1)
        for i in range(times):
            self._tap(keys)
            if i + 1 < times:
                self.sleep(0.03)

    # логика

    def op_wait(self, s):
        self.sleep(self.num(s.get("seconds"), "пауза", minimum=0, maximum=86400))

    def op_set_vars(self, s):
        for a in s.get("assignments") or []:
            self.vars[a["target"]] = self.ev(a["expr"], f"«{a['target']}»")

    def op_log(self, s):
        self.emit({"type": "log", "node": self.node, "message": self.text(s.get("message"), "сообщение")[:2000]})

    def op_if(self, s):
        branch = s["then"] if self.ev(s["expr"], "условие") else s["else"]
        self.block(branch)

    def op_repeat(self, s):
        count = self.num(s.get("count"), "число повторов", integer=True, minimum=0, maximum=MAX_REPEAT)
        var = s.get("var")
        node = self.node
        for i in range(1, count + 1):
            if var:
                self.vars[var] = i
            self.block(s["body"])
            self.node = node

    def op_while(self, s):
        limit = self.num(s.get("max_iterations"), "предел повторов", integer=True, minimum=1,
                         maximum=MAX_REPEAT, default=1000)
        var = s.get("var")
        node = self.node
        i = 0
        while True:
            self.node = node
            if not self.ev(s["expr"], "условие цикла"):
                break
            i += 1
            if i > limit:
                raise StepError(node, f"цикл «пока» превысил {limit} повторов")
            if var:
                self.vars[var] = i
            self.block(s["body"])

    def op_stop(self, s):
        raise Finish(s.get("status") or "ok", self.text(s.get("message") or "", "сообщение"))

    # шаги, которые выполняет сервер платформы (песочница и firewall проекта)

    def _server(self, op, payload):
        if self.server_op is None:
            raise StepError(self.node, "шаг выполняется на сервере платформы, а связи с ним нет")
        try:
            return self.server_op(op, self.node, payload)
        except Stopped:
            raise
        except Exception as e:  # noqa: BLE001
            raise StepError(self.node, str(e)) from None

    def op_code(self, s):
        result = self._server("code", {"data": jsonable(self.vars)})
        for key in s.get("outputs") or []:
            if key in result:
                self.vars[key] = result[key]

    def op_http(self, s):
        payload = {"url": self.text(s.get("url"), "адрес")}
        body = {}
        for item in s.get("body") or []:
            body[item["key"]] = jsonable(self.ev(item["expr"], f"поле «{item['key']}»"))
        payload["body"] = body
        result = self._server("http", payload)
        self.vars[s.get("target") or "response"] = result
