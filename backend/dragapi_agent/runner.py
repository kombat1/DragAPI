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


class LimitError(StepError):
    """Лимит времени или действий сценария — не повторяется и не уходит в ветку «ошибка»."""


MAX_CALL_DEPTH = 20


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
        self.depth = 0          # вложенность подпрограмм («Выполнить сценарий»)

    # --- служебное ---

    def check(self):
        if self.stop.is_set():
            raise Stopped("остановлено")
        if time.monotonic() - self.started > self.limits["max_seconds"]:
            raise LimitError(self.node, f"сценарий идёт дольше {self.limits['max_seconds']} с — остановлен лимитом")

    def act(self, n: int = 1):
        """Перед каждым действием ввода: стоп, лимиты, аварийный угол."""
        self.check()
        self.actions += n
        if self.actions > self.limits["max_actions"]:
            raise LimitError(self.node, f"больше {self.limits['max_actions']} действий — остановлено лимитом")
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
        """Координаты шага или None, если обе пустые (действие в текущей позиции).
        С «Внутри окна» — относительно левого верхнего угла окна, где бы оно сейчас ни было."""
        x, y = step.get(xk), step.get(yk)
        if (x is None or str(x).strip() == "") and (y is None or str(y).strip() == ""):
            return None
        p = (self.num(x, f"координата {xk}", integer=True), self.num(y, f"координата {yk}", integer=True))
        win = self.window_of(step)
        return (p[0] + win["x"], p[1] + win["y"]) if win else p

    def window_of(self, step) -> dict | None:
        """Окно из переменной («Найти окно») — с текущими координатами: его могли сдвинуть."""
        name = step.get("window")
        if not name:
            return None
        saved = self.vars.get(name)
        if not isinstance(saved, dict) or "id" not in saved:
            raise StepError(self.node, f"в переменной «{name}» нет окна — сначала блок «Найти окно» с этой переменной")
        info = self._window_call(lambda: self.d.get_window(saved["id"]))
        if info is None:
            raise StepError(self.node, f"окно «{saved.get('title', '')}» закрыто")
        if info.get("state") == "minimized":
            raise StepError(self.node, f"окно «{info.get('title', '')}» свёрнуто — разверните его блоком «Окно: действие»")
        self.vars[name] = {**saved, **info}
        return info

    def _window_call(self, fn):
        from .windows import WindowError
        if not hasattr(self.d, "list_windows"):
            raise StepError(self.node, "этот агент не умеет работать с окнами — обновите агента")
        try:
            return fn()
        except WindowError as e:
            raise StepError(self.node, str(e)) from None

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
            policy = step.get("on_error")
            failed = self._attempts(handler, step, policy) if policy else self._exec(handler, step)
            if failed is not None:
                if policy.get("then") == "branch" and step.get("on_fail") is not None:
                    self.block(step["on_fail"])
                    return                   # продолжение — в ветке «ошибка»
                if policy.get("then") != "skip":
                    raise failed
            self.node = step.get("node")
            if "next" in step:               # шаг с веткой «ошибка»: продолжение после успеха
                self.block(step["next"])
                return

    def _exec(self, handler, step):
        try:
            handler(step)
        except (Stopped, StepError, Finish):
            raise
        except Exception as e:  # noqa: BLE001 — сбой драйвера ввода и т.п.: показать, в каком блоке
            raise StepError(self.node, f"{type(e).__name__}: {e}") from None
        return None

    def _attempts(self, handler, step, policy) -> StepError | None:
        """«Если шаг не удался»: повторить N раз с паузой. Повторяется только ошибка самого шага —
        не лимиты и не ошибки внутри его веток. -> None (получилось) или последняя ошибка."""
        node, op = step.get("node"), step["op"]
        attempts = 1 + int(policy.get("retries") or 0)
        error = None
        for attempt in range(1, attempts + 1):
            self.node = node
            try:
                self._exec(handler, step)
                if attempt > 1:
                    self.emit({"type": "step", "node": node, "op": op, "status": "recovered", "attempt": attempt,
                               "of": attempts, "message": f"получилось с {attempt}-й попытки"})
                return None
            except LimitError:
                raise
            except StepError as e:
                if e.node != node:
                    raise
                error = e
            if attempt < attempts:
                self.emit({"type": "step", "node": node, "op": op, "status": "retry", "attempt": attempt,
                           "of": attempts, "message": f"попытка {attempt} из {attempts}: {error}"})
                self.sleep(float(policy.get("delay") or 0))
        self.node = node
        self.vars[policy.get("var") or "error"] = {"message": str(error), "node": node, "block": op,
                                                    "attempts": attempts}
        then = policy.get("then") or "stop"
        if then != "stop" and not (then == "branch" and step.get("on_fail") is None):
            self.emit({"type": "step", "node": node, "op": op, "status": "failed", "then": then,
                       "message": f"{error} — {'пропускаю шаг' if then == 'skip' else 'иду по ветке «ошибка»'}"})
        return error

    # мышь

    def op_mouse_move(self, s):
        target = self.point({**s, "window": "" if s.get("relative") else s.get("window")})
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

    def op_call_scenario(self, s):
        """Подпрограмма: свои переменные (только параметры), «Завершить» с успехом — возврат.
        Сбой внутри — сбой этого блока (к нему применяется его «Если шаг не удался»)."""
        name = s.get("scenario")
        sub = (self.scenario.get("subs") or {}).get(name)
        if sub is None:
            raise StepError(self.node, f"сценарий «{name}» не найден — обновите агента или проверьте проект")
        if self.depth >= MAX_CALL_DEPTH:
            raise StepError(self.node, f"слишком глубокая вложенность подпрограмм (больше {MAX_CALL_DEPTH})")
        node = self.node
        inputs = {p: None for p in sub.get("params") or []}
        for item in s.get("params") or []:
            inputs[item["key"]] = self.ev(item["expr"], f"параметр «{item['key']}»")
        caller = self.vars
        self.vars, self.depth = inputs, self.depth + 1
        self.emit({"type": "log", "node": node, "message": f"↪ сценарий «{name}»"})
        try:
            self.block(sub.get("steps") or [])
        except Finish as f:
            if f.status != "ok":
                raise StepError(node, f"сценарий «{name}» завершился с ошибкой{': ' + f.message if f.message else ''}") from None
        except LimitError:
            raise
        except StepError as e:
            self.emit({"type": "step", "node": e.node, "status": "error", "message": str(e)})
            raise StepError(node, f"сценарий «{name}»: {e}") from None
        finally:
            result, self.vars, self.depth, self.node = self.vars, caller, self.depth - 1, node
        if s.get("target"):
            self.vars[s["target"]] = result

    def op_stop(self, s):
        raise Finish(s.get("status") or "ok", self.text(s.get("message") or "", "сообщение"))

    # зрение: снимок экрана → поиск картинки, цвет, текст

    def _vision(self):
        from . import vision
        return vision

    def _region(self, s):
        """Область поиска в координатах экрана; «Внутри окна» — относительно окна (пусто — всё окно)."""
        r = s.get("region")
        win = self.window_of(s)
        if win is None:
            return (r["x"], r["y"], r["w"], r["h"]) if r else None
        if not r:
            return (win["x"], win["y"], win["w"], win["h"])
        return (win["x"] + r["x"], win["y"] + r["y"], r["w"], r["h"])

    def _poll(self, s, look):
        """look() до успеха или пока не истечёт «Ждать до, с» (0 — проверить один раз)."""
        wait = self.num(s.get("wait"), "ждать", minimum=0, maximum=86400, default=0)
        end = time.monotonic() + wait
        while True:
            self.check()
            if look():
                return True
            if time.monotonic() >= end:
                return False
            self.sleep(min(0.25, max(0.0, end - time.monotonic())))

    def _shot(self, region):
        """Снимок (области) экрана и смещение его левого верхнего угла в координатах экрана."""
        v = self._vision()
        ox, oy = self.d.origin() if hasattr(self.d, "origin") else (0, 0)
        if region:
            region = (region[0] - ox, region[1] - oy, region[2], region[3])
        try:
            img, (x, y) = v.crop(self.d.screenshot(), region)
        except v.VisionError as e:
            raise StepError(self.node, str(e)) from None
        return img, (x + ox, y + oy)

    def op_image_find(self, s):
        v = self._vision()
        try:
            template = v.decode_image(s.get("image"))
        except v.VisionError as e:
            raise StepError(self.node, str(e)) from None
        conf = self.num(s.get("confidence"), "точность", minimum=0.5, maximum=1, default=0.85)
        region, mode = self._region(s), s.get("mode") or "appear"
        best: dict = {}

        def look():
            img, (ox, oy) = self._shot(region)
            m = v.find_template(img, template, grayscale=bool(s.get("grayscale")))
            m.update(left=m["left"] + ox, top=m["top"] + oy, x=m["x"] + ox, y=m["y"] + oy)
            best.clear()
            best.update(m)
            return (m["score"] >= conf) == (mode == "appear")

        ok = self._poll(s, look)
        found = best.get("score", 0) >= conf
        info = {"found": found, **best}
        if s.get("target"):
            self.vars[s["target"]] = info
        score = f"сходство {best.get('score', 0):.2f}"
        if mode == "appear":
            self.emit({"type": "log", "node": self.node, "message":
                       f"найдено в {best['x']},{best['y']} ({score})" if found else
                       f"не найдено: лучшее {score} < {conf:g}"})
        else:
            self.emit({"type": "log", "node": self.node, "message":
                       "картинка исчезла" if ok else f"картинка всё ещё на экране ({score})"})
        action = s.get("action") or "none"
        if ok and found and action != "none":
            dx = self.num(s.get("dx"), "смещение X", integer=True, default=0)
            dy = self.num(s.get("dy"), "смещение Y", integer=True, default=0)
            self.glide((best["x"] + dx, best["y"] + dy), self.num(s.get("duration"), "перемещение", minimum=0,
                                                                 maximum=60, default=0))
            if action != "move":
                self.act()
                self.d.click("right" if action == "right" else "left", 2 if action == "double" else 1)
        self.block(s["then"] if ok else s["else"])

    def op_pixel_check(self, s):
        v = self._vision()
        x, y = self.point(s)
        try:
            want = v.parse_color(self.text(s.get("color"), "цвет"))
        except v.VisionError as e:
            raise StepError(self.node, str(e)) from None
        tol = self.num(s.get("tolerance"), "допуск", integer=True, minimum=0, maximum=255, default=10)
        seen: dict = {}

        def look():
            img, _ = self._shot((x, y, 1, 1))   # точка за краем экрана → ошибка «за пределами»
            rgb = v.pixel_rgb(img, 0, 0)
            seen.update(v.color_info(rgb), distance=v.color_distance(rgb, want))
            return seen["distance"] <= tol

        ok = self._poll(s, look)
        if s.get("target"):
            self.vars[s["target"]] = {**seen, "match": ok}
        self.emit({"type": "log", "node": self.node,
                   "message": f"цвет {seen.get('hex')} в {x},{y} — {'совпадает' if ok else 'не совпадает'}"
                              f" (отличие {seen.get('distance')}, допуск {tol})"})
        self.block(s["then"] if ok else s["else"])

    def op_ocr(self, s):
        v = self._vision()
        region = self._region(s)
        img, _ = self._shot(region)
        try:
            text = v.ocr(img, s.get("lang") or "rus+eng", digits=bool(s.get("digits")))
        except v.VisionError as e:
            raise StepError(self.node, str(e)) from None
        if s.get("digits"):
            text = text.replace(" ", "")
        self.vars[s.get("target") or "text"] = text
        self.emit({"type": "log", "node": self.node, "message": f"текст: {text[:300]!r}"})

    # окна

    def op_window_find(self, s):
        from .windows import matches
        title = self.text(s.get("title"), "заголовок")
        how, process = s.get("match") or "contains", self.text(s.get("process") or "", "программа").strip()
        mode = s.get("mode") or "appear"
        found: dict = {}

        def look():
            wins = self._window_call(self.d.list_windows)
            hit = next((w for w in wins if self._window_call(lambda w=w: matches(w, title, how, process))), None)
            found.clear()
            if hit:
                found.update(hit)
            return bool(hit) == (mode == "appear")

        ok = self._poll(s, look)
        what = f"«{title}»" + (f" ({process})" if process else "")
        if mode == "appear" and ok and s.get("activate", True):
            self._window_call(lambda: self.d.window_do(found["id"], "activate"))
            from .windows import wait_state
            info = self._window_call(lambda: wait_state(self.d.get_window, found["id"],
                                                        lambda i: i.get("active") and i.get("state") != "minimized"))
            found.update(info or {})
        if s.get("target"):
            self.vars[s["target"]] = {"found": bool(found), **found}
        self.emit({"type": "log", "node": self.node, "message":
                   (f"окно {what} закрылось" if ok else f"окно {what} всё ещё открыто") if mode == "disappear" else
                   (f"окно «{found.get('title')}» {found.get('w')}×{found.get('h')} в {found.get('x')},{found.get('y')}"
                    if ok else f"окна {what} нет")})
        self.block(s["then"] if ok else s["else"])

    def op_window_action(self, s):
        from .windows import wait_state
        name = s.get("window") or "win"
        saved = self.vars.get(name)
        if not isinstance(saved, dict) or "id" not in saved:
            raise StepError(self.node, f"в переменной «{name}» нет окна — сначала блок «Найти окно» с этой переменной")
        wid, action = saved["id"], s.get("action") or "activate"
        if self._window_call(lambda: self.d.get_window(wid)) is None:
            raise StepError(self.node, f"окно «{saved.get('title', '')}» закрыто")
        geo = {}
        if action == "move":
            for k in ("x", "y", "w", "h"):
                if str(s.get(k) or "").strip():
                    geo[k] = self.num(s.get(k), {"x": "X", "y": "Y", "w": "ширина", "h": "высота"}[k], integer=True,
                                      minimum=1 if k in ("w", "h") else None)
            if not geo:
                raise StepError(self.node, "задайте положение и/или размер окна")
        self.act()
        self._window_call(lambda: self.d.window_do(wid, action, **geo))
        check = {"activate": lambda i: i.get("active"), "minimize": lambda i: i.get("state") == "minimized",
                 "maximize": lambda i: i.get("state") == "maximized", "restore": lambda i: i.get("state") == "normal",
                 "move": lambda i: all(i.get(k) == v for k, v in geo.items()), "close": lambda i: False}[action]
        info = self._window_call(lambda: wait_state(self.d.get_window, wid, check, 3.0 if action == "close" else 1.5))
        if action == "close":
            self.vars[name] = {**saved, "found": info is not None, "closed": info is None}
            self.emit({"type": "log", "node": self.node, "message":
                       "окно закрыто" if info is None else "окно не закрылось (возможно, спрашивает о сохранении)"})
            return
        if info is not None:
            self.vars[name] = {**saved, **info}
        label = {"activate": "активно", "minimize": "свёрнуто", "maximize": "развёрнуто", "restore": "восстановлено",
                 "move": "перемещено"}[action]
        self.emit({"type": "log", "node": self.node, "message": f"окно «{saved.get('title', '')}» {label}"
                   + (f": {info['w']}×{info['h']} в {info['x']},{info['y']}" if info and action in ("move", "maximize", "restore") else "")})

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

    def op_telegram(self, s):
        self._server("tg_send", {"text": self.text(s.get("text"), "текст")[:4000]})

    def op_tg_ask(self, s):
        timeout = self.num(s.get("timeout"), "ожидание ответа", minimum=5, maximum=86400, default=300)
        reply = self._server("tg_ask", {"text": self.text(s.get("text"), "вопрос")[:4000], "timeout": timeout,
                                        "_timeout": timeout}) or {}
        answer = reply.get("answer")
        if s.get("target"):
            self.vars[s["target"]] = answer
        self.emit({"type": "log", "node": self.node, "message":
                   "ответа нет — ветка «нет»" if answer is None else
                   f"ответ: {'да' if answer else 'нет'}{' (' + reply['by'] + ')' if reply.get('by') else ''}"})
        self.block(s["then"] if answer else s["else"])

    def op_http(self, s):
        payload = {"url": self.text(s.get("url"), "адрес")}
        body = {}
        for item in s.get("body") or []:
            body[item["key"]] = jsonable(self.ev(item["expr"], f"поле «{item['key']}»"))
        payload["body"] = body
        result = self._server("http", payload)
        self.vars[s.get("target") or "response"] = result
