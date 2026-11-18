"""Агент: принимает команды платформы и выполняет их на этом компьютере.

Не зависит от транспорта: платформа передаёт сообщения в handle(), агент отвечает через send().
Локальный агент живёт внутри сервера платформы, удалённый — в dragapi_agent/__main__.py (WebSocket).

Сообщения платформы → агент:
    run {run_id, scenario, env, inputs, limits}   запустить сценарий
    stop {run_id}                                 остановить
    op_result {req, ok, result | error}           ответ на шаг, выполненный сервером (код, HTTP)
    hotkeys {items: [{keys, pid, trigger}]}        горячие клавиши включённых сценариев
    record_start {rec_id, countdown} / record_stop {rec_id}
    pick_point {req, countdown}                   через N секунд сообщить позицию курсора
    screenshot {req, countdown}                   через N секунд снять экран (для образцов и областей)
    vision_test {req, step}                       проверить блок «Зрение» на текущем экране, без кликов
    list_windows {req}                            открытые окна (для выбора заголовка в редакторе)
Агент → платформа: hello, event, done, op, hotkey, recorded, record_state, point, shot, vision_result,
windows, stopped_by_user.
"""

from __future__ import annotations

import itertools
import platform
import shutil
import subprocess
import threading
import time
from typing import Callable

from . import VERSION
from .driver import DriverError, PynputDriver
from .keys import KeyError_, MODIFIERS, SPECIAL, parse_combo
from .runner import Runner

EVENTS_PER_SECOND = 100
OP_TIMEOUT = 60


def _key_name(key) -> str | None:
    """Клавиша pynput → имя как в keys.py (модификаторы без _l/_r, буквы как есть)."""
    try:
        from pynput.keyboard import Key, KeyCode
    except Exception:  # noqa: BLE001
        return None
    if isinstance(key, Key):
        name = key.name
        for base, out in (("ctrl", "ctrl"), ("shift", "shift"), ("cmd", "super"), ("super", "super")):
            if name.startswith(base):
                return out
        if name in ("alt", "alt_l", "alt_r"):
            return "alt"
        return name if name in SPECIAL else None
    if isinstance(key, KeyCode):
        ch = key.char
        if ch and ord(ch) < 32 and 1 <= ord(ch) <= 26:   # Ctrl+буква на некоторых системах даёт управляющий код
            return chr(ord(ch) + 96)
        if ch:
            return ch
        vk = getattr(key, "vk", None)
        if vk is not None and 32 < vk < 127:
            return chr(vk).lower()
    return None


class Agent:
    def __init__(self, send: Callable[[dict], None] | None = None, *, name: str = "",
                 driver_factory=PynputDriver, stop_hotkey: str = "ctrl+alt+esc",
                 listeners: bool = True, notify: bool = True):
        self.send_fn = send
        self.name = name or platform.node()
        self.driver_factory = driver_factory
        self.stop_keys = parse_combo(stop_hotkey)
        self.use_listeners = listeners
        self.notify_enabled = notify and bool(shutil.which("notify-send"))
        self.lock = threading.RLock()
        self.driver = None
        self.driver_error: str | None = None
        self.run_id: str | None = None
        self.run_stop: threading.Event | None = None
        self.run_name = ""
        self.pending: dict[int, list] = {}
        self.req_ids = itertools.count(1)
        self.hotkeys: dict[frozenset, tuple[str, str]] = {}
        self.pressed: set[str] = set()
        self.recording: dict | None = None
        self.kb_listener = None
        self.mouse_listener = None
        self._bucket = [EVENTS_PER_SECOND, time.monotonic()]
        self._dropped = 0

    # --- связь ---

    def send(self, msg: dict):
        fn = self.send_fn
        if fn is None:
            return
        try:
            fn(msg)
        except Exception:  # noqa: BLE001 — связь оборвалась: сообщение теряется, переподключение — у транспорта
            pass

    def get_driver(self):
        if self.driver is None and self.driver_error is None:
            try:
                self.driver = self.driver_factory()
            except DriverError as e:
                self.driver_error = str(e)
        if self.driver is None:
            raise DriverError(self.driver_error or "управление вводом недоступно")
        return self.driver

    def hello(self) -> dict:
        screen = None
        try:
            screen = self.get_driver().screen_size()
        except DriverError:
            pass
        return {"type": "hello", "name": self.name, "version": VERSION,
                "platform": f"{platform.system()} {platform.release()}", "screen": screen,
                "stop_hotkey": "+".join(self.stop_keys), "error": self.driver_error,
                "capabilities": ["input", "record", "hotkeys", "pick_point", "vision", "windows"]}

    def start(self):
        """Глобальный слушатель клавиатуры: аварийная остановка и горячие клавиши сценариев."""
        if not self.use_listeners or self.kb_listener is not None:
            return
        try:
            self.get_driver()
            from pynput import keyboard
            self.kb_listener = keyboard.Listener(on_press=self._on_press, on_release=self._on_release)
            self.kb_listener.daemon = True
            self.kb_listener.start()
        except Exception as e:  # noqa: BLE001
            self.driver_error = self.driver_error or f"глобальные клавиши недоступны: {e}"

    def shutdown(self):
        self.on_disconnect()
        for listener in (self.kb_listener, self.mouse_listener):
            if listener is not None:
                try:
                    listener.stop()
                except Exception:  # noqa: BLE001
                    pass
        self.kb_listener = self.mouse_listener = None

    def on_disconnect(self):
        """Нет связи с платформой — ничего не выполняем вслепую."""
        with self.lock:
            if self.run_stop is not None:
                self.run_stop.set()
            for item in self.pending.values():
                item[1] = {"ok": False, "error": "связь с платформой потеряна"}
                item[0].set()
            if self.recording is not None:
                self._stop_mouse_listener()
                self.recording = None

    def notify(self, text: str):
        if self.notify_enabled:
            try:
                subprocess.Popen(["notify-send", "-a", "DragAPI", "-t", "4000", "DragAPI", text],
                                 stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
            except OSError:
                pass

    # --- входящие сообщения ---

    def handle(self, msg: dict):
        kind = msg.get("type")
        if kind == "run":
            self._start_run(msg)
        elif kind == "stop":
            with self.lock:
                if self.run_stop is not None and msg.get("run_id") in (None, self.run_id):
                    self.run_stop.set()
        elif kind == "op_result":
            with self.lock:
                item = self.pending.get(msg.get("req"))
            if item:
                item[1] = msg
                item[0].set()
        elif kind == "hotkeys":
            table = {}
            for it in msg.get("items") or []:
                try:
                    keys = parse_combo(it["keys"])
                except (KeyError_, KeyError, TypeError):
                    continue
                table[frozenset(keys)] = (it.get("pid"), it.get("trigger"))
            with self.lock:
                self.hotkeys = table
        elif kind == "record_start":
            threading.Thread(target=self._record_start, args=(msg,), daemon=True).start()
        elif kind == "record_stop":
            self._record_stop(msg.get("rec_id"), by_user=False)
        elif kind == "pick_point":
            threading.Thread(target=self._pick_point, args=(msg,), daemon=True).start()
        elif kind == "screenshot":
            threading.Thread(target=self._screenshot, args=(msg,), daemon=True).start()
        elif kind == "vision_test":
            threading.Thread(target=self._vision_test, args=(msg,), daemon=True).start()
        elif kind == "list_windows":
            threading.Thread(target=self._list_windows, args=(msg,), daemon=True).start()

    # --- запуск сценария ---

    def _emit(self, run_id, event):
        now = time.monotonic()
        tokens, last = self._bucket
        tokens = min(EVENTS_PER_SECOND, tokens + (now - last) * EVENTS_PER_SECOND)
        if event.get("status") == "error" or event.get("type") == "log" or tokens >= 1:
            self._bucket = [max(tokens - 1, 0), now]
            self.send({"type": "event", "run_id": run_id, "event": {**event, "t": time.time()}})
        else:
            self._bucket = [tokens, now]
            self._dropped += 1

    def _start_run(self, msg):
        run_id = msg.get("run_id")
        with self.lock:
            if self.run_id is not None:
                self.send({"type": "done", "run_id": run_id, "status": "error",
                           "message": f"агент занят сценарием «{self.run_name}»", "vars": {}, "actions": 0})
                return
            if self.recording is not None:
                self.send({"type": "done", "run_id": run_id, "status": "error",
                           "message": "на агенте идёт запись действий", "vars": {}, "actions": 0})
                return
            self.run_id, self.run_stop = run_id, threading.Event()
            self.run_name = (msg.get("scenario") or {}).get("name", "")
        threading.Thread(target=self._run, args=(msg, self.run_stop), daemon=True, name=f"run-{run_id}").start()

    def _server_op(self, run_id, stop: threading.Event):
        def call(op, node, payload):
            req = next(self.req_ids)
            item = [threading.Event(), None]
            with self.lock:
                self.pending[req] = item
            self.send({"type": "op", "run_id": run_id, "req": req, "op": op, "node": node, "payload": payload})
            # шаги вроде «Спросить в Telegram» ждут человека — дольше обычного
            deadline = time.monotonic() + max(OP_TIMEOUT, float(payload.get("_timeout") or 0) + 15)
            try:
                while not item[0].wait(0.05):
                    if stop.is_set():
                        from .runner import Stopped
                        raise Stopped("остановлено")
                    if time.monotonic() > deadline:
                        raise RuntimeError("сервер платформы не ответил вовремя")
            finally:
                with self.lock:
                    self.pending.pop(req, None)
            reply = item[1] or {}
            if not reply.get("ok"):
                raise RuntimeError(reply.get("error") or "ошибка на сервере платформы")
            return reply.get("result")
        return call

    def _run(self, msg, stop: threading.Event):
        run_id = msg.get("run_id")
        self._dropped = 0
        try:
            driver = self.get_driver()
        except DriverError as e:
            result = {"status": "error", "message": str(e), "vars": {}, "actions": 0}
        else:
            name = (msg.get("scenario") or {}).get("name", "")
            self.notify(f"Сценарий «{name}» запущен. Остановить: {'+'.join(self.stop_keys).title()}")
            runner = Runner(msg.get("scenario") or {}, driver, env=msg.get("env"), inputs=msg.get("inputs"),
                            emit=lambda e: self._emit(run_id, e), server_op=self._server_op(run_id, stop),
                            limits=msg.get("limits"), stop_event=stop)
            result = runner.run()
            if result["status"] == "stopped":
                self.notify(f"Сценарий «{name}» остановлен: {result['message']}")
            elif result["status"] == "error":
                self.notify(f"Сценарий «{name}»: ошибка — {result['message'][:200]}")
        with self.lock:
            self.run_id = self.run_stop = None
            self.run_name = ""
        self.send({"type": "done", "run_id": run_id, "events_dropped": self._dropped, **result})

    # --- глобальные клавиши ---

    def _on_press(self, key):
        name = _key_name(key)
        if name is None:
            return
        low = name.lower() if len(name) == 1 else name
        self.pressed.add(low)
        rec = self.recording
        if rec is not None and rec.get("active"):
            rec["events"].append({"t": time.monotonic() - rec["t0"], "kind": "key_down", "key": name})
        if low in MODIFIERS:
            return
        combo = frozenset(k for k in self.pressed if k in MODIFIERS) | {low}
        if combo == frozenset(self.stop_keys):
            self._user_stop()
            return
        with self.lock:
            target = self.hotkeys.get(combo)
        if target and self.recording is None:
            self.send({"type": "hotkey", "pid": target[0], "trigger": target[1]})

    def _on_release(self, key):
        name = _key_name(key)
        if name is None:
            return
        low = name.lower() if len(name) == 1 else name
        self.pressed.discard(low)
        rec = self.recording
        if rec is not None and rec.get("active") and low in MODIFIERS:
            rec["events"].append({"t": time.monotonic() - rec["t0"], "kind": "key_up", "key": low})

    def _user_stop(self):
        """Аварийная клавиша: остановить сценарий и запись."""
        with self.lock:
            running = self.run_stop is not None
            if running:
                self.run_stop.set()
            rec = self.recording
        if rec is not None:
            self._record_stop(rec["rec_id"], by_user=True)
        if running:
            self.send({"type": "stopped_by_user"})

    # --- запись ---

    def _record_start(self, msg):
        rec_id = msg.get("rec_id")
        with self.lock:
            if self.run_id is not None or self.recording is not None:
                self.send({"type": "record_state", "rec_id": rec_id, "state": "error",
                           "message": "агент занят: идёт сценарий или другая запись"})
                return
            self.recording = {"rec_id": rec_id, "events": [], "active": False, "t0": 0.0}
        try:
            self.get_driver()
            mouse = None
            if self.use_listeners:   # без слушателей (тесты) события подкладываются вручную
                from pynput import mouse
        except Exception as e:  # noqa: BLE001
            with self.lock:
                self.recording = None
            self.send({"type": "record_state", "rec_id": rec_id, "state": "error", "message": str(e)})
            return
        countdown = max(0, min(int(msg.get("countdown") or 0), 10))
        for left in range(countdown, 0, -1):
            self.send({"type": "record_state", "rec_id": rec_id, "state": "countdown", "left": left})
            time.sleep(1)
            if self.recording is None or self.recording.get("rec_id") != rec_id:
                return
        rec = self.recording
        rec["t0"] = time.monotonic()
        rec["active"] = True

        def on_click(x, y, button, pressed):
            if rec.get("active"):
                rec["events"].append({"t": time.monotonic() - rec["t0"], "kind": "down" if pressed else "up",
                                      "button": getattr(button, "name", "left"), "x": int(x), "y": int(y)})

        def on_scroll(x, y, dx, dy):
            if rec.get("active"):
                rec["events"].append({"t": time.monotonic() - rec["t0"], "kind": "scroll",
                                      "dx": int(dx), "dy": int(dy), "x": int(x), "y": int(y)})

        if mouse is not None:
            self.mouse_listener = mouse.Listener(on_click=on_click, on_scroll=on_scroll)
            self.mouse_listener.daemon = True
            self.mouse_listener.start()
            self.start()  # клавиатура пишется общим слушателем
        self.notify(f"Идёт запись действий. Закончить: {'+'.join(self.stop_keys).title()} "
                    f"или кнопкой в платформе")
        self.send({"type": "record_state", "rec_id": rec_id, "state": "recording"})

    def _stop_mouse_listener(self):
        if self.mouse_listener is not None:
            try:
                self.mouse_listener.stop()
            except Exception:  # noqa: BLE001
                pass
            self.mouse_listener = None

    def _record_stop(self, rec_id, by_user: bool):
        with self.lock:
            rec = self.recording
            if rec is None or (rec_id is not None and rec.get("rec_id") != rec_id):
                return
            self.recording = None
        rec["active"] = False
        self._stop_mouse_listener()
        events = rec["events"]
        if by_user:  # нажатия самой аварийной клавиши в запись не попадают
            stop = set(self.stop_keys)
            while events and events[-1].get("kind") in ("key_down", "key_up") and \
                    (events[-1].get("key") or "").lower() in stop:
                events.pop()
        self.send({"type": "recorded", "rec_id": rec["rec_id"], "events": events, "by_user": by_user,
                   "stopped_at": time.monotonic() - rec["t0"] if rec.get("t0") else 0})
        self.notify("Запись действий закончена")

    # --- позиция курсора ---

    def _pick_point(self, msg):
        req = msg.get("req")
        countdown = max(0, min(int(msg.get("countdown") or 0), 10))
        try:
            driver = self.get_driver()
        except DriverError as e:
            self.send({"type": "point", "req": req, "error": str(e)})
            return
        for left in range(countdown, 0, -1):
            self.send({"type": "point", "req": req, "left": left})
            time.sleep(1)
        x, y = driver.position()
        self.send({"type": "point", "req": req, "x": x, "y": y, "window": self._window_at(driver, x, y)})

    def _windows_of(self, driver) -> list[dict] | None:
        from .windows import WindowError
        try:
            return driver.list_windows() if hasattr(driver, "list_windows") else None
        except WindowError:
            return None

    def _window_at(self, driver, x, y) -> dict | None:
        """Окно под точкой — редактор пересчитает координаты «внутри окна»."""
        from .windows import at_point
        wins = self._windows_of(driver)
        return at_point(wins, x, y) if wins else None

    def _list_windows(self, msg):
        from .windows import WindowError
        req = msg.get("req")
        try:
            driver = self.get_driver()
            items = driver.list_windows()
        except (DriverError, WindowError) as e:
            self.send({"type": "windows", "req": req, "error": str(e)})
            return
        self.send({"type": "windows", "req": req, "items": items})

    # --- зрение: снимок экрана и проверка блока ---

    MAX_SHOT_CHARS = 12_000_000

    def _screenshot(self, msg):
        from .vision import VisionError, encode_jpeg, encode_png
        req = msg.get("req")
        countdown = max(0, min(int(msg.get("countdown") or 0), 10))
        try:
            driver = self.get_driver()
            for left in range(countdown, 0, -1):
                self.send({"type": "shot", "req": req, "left": left})
                time.sleep(1)
            img = driver.screenshot()
            data = encode_png(img)
            if len(data) > self.MAX_SHOT_CHARS:      # огромный экран: JPEG, чтобы влезть в сообщение
                data = encode_jpeg(img, 92)
            h, w = img.shape[:2]
            ox, oy = driver.origin() if hasattr(driver, "origin") else (0, 0)
            wins = [w for w in self._windows_of(driver) or [] if w.get("state") != "minimized"]
            self.send({"type": "shot", "req": req, "data": data, "w": w, "h": h, "ox": ox, "oy": oy, "left": 0,
                       "windows": wins})
        except (DriverError, VisionError) as e:
            self.send({"type": "shot", "req": req, "error": str(e)})

    def _vision_test(self, msg):
        """Один блок «Зрение» на текущем экране: без кликов и ожидания — что нашёл и с каким сходством."""
        req = msg.get("req")
        step = dict(msg.get("step") or {})
        if step.get("op") not in ("image_find", "pixel_check", "ocr", "window_find"):
            self.send({"type": "vision_result", "req": req, "error": "это не блок «Зрение»"})
            return
        step.update(action="none", activate=False, wait="0", then=[], **({"else": []} if step.get("op") != "ocr" else {}))
        logs = []
        try:
            driver = self.get_driver()
        except DriverError as e:
            self.send({"type": "vision_result", "req": req, "error": str(e)})
            return
        runner = Runner({"steps": [step]}, driver, env=msg.get("env"), inputs=msg.get("inputs"),
                        emit=lambda e: e.get("type") == "log" and logs.append(e.get("message")),
                        limits={"max_seconds": 60, "failsafe_corner": False})
        result = runner.run()
        self.send({"type": "vision_result", "req": req, "status": result["status"],
                   "message": result["message"] or "; ".join(logs), "vars": result["vars"]})
