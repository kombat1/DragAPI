"""Драйверы ввода: PynputDriver управляет настоящими мышью и клавиатурой, FakeDriver — для тестов."""

from __future__ import annotations

import os
import sys

BUTTONS = ["left", "right", "middle"]


class DriverError(RuntimeError):
    pass


class FakeDriver:
    """Ничего не нажимает — только записывает действия (тесты и пробные прогоны)."""

    def __init__(self, size=(1920, 1080), pos=(500, 500)):
        self.size = size
        self.pos = tuple(pos)
        self.actions: list[tuple] = []
        self.held: set[str] = set()
        self.screen = None          # тесты подкладывают «экран» — массив BGR (numpy)
        self.shots = 0

    def screenshot(self):
        from .vision import VisionError
        if self.screen is None:
            raise VisionError("у тестового драйвера нет экрана")
        self.shots += 1
        return self.screen() if callable(self.screen) else self.screen

    def screen_size(self):
        return self.size

    def position(self):
        return self.pos

    def move(self, x, y):
        self.pos = (x, y)
        self.actions.append(("move", x, y))

    def press(self, button):
        self.actions.append(("press", button, *self.pos))

    def release(self, button):
        self.actions.append(("release", button, *self.pos))

    def click(self, button, count=1):
        self.actions.append(("click", button, count, *self.pos))

    def scroll(self, dx, dy):
        self.actions.append(("scroll", dx, dy, *self.pos))

    def key_down(self, key):
        self.held.add(key)
        self.actions.append(("key_down", key))

    def key_up(self, key):
        self.held.discard(key)
        self.actions.append(("key_up", key))

    def type_char(self, ch):
        self.actions.append(("type", ch))

    def moves(self):
        return [a for a in self.actions if a[0] == "move"]

    def without_moves(self):
        return [a for a in self.actions if a[0] != "move"]


class PynputDriver:
    def __init__(self):
        if sys.platform.startswith("linux") and not os.environ.get("DISPLAY"):
            raise DriverError("нет графического сеанса X11 (переменная DISPLAY не задана)")
        try:
            from pynput import keyboard, mouse
        except Exception as e:  # noqa: BLE001 — нет X-сервера, нет библиотеки и т.п.
            raise DriverError(f"управление вводом недоступно: {e}") from None
        self._keyboard, self._mouse_mod = keyboard, mouse
        self.m = mouse.Controller()
        self.k = keyboard.Controller()
        self._buttons = {"left": mouse.Button.left, "right": mouse.Button.right, "middle": mouse.Button.middle}

    def screen_size(self):
        try:
            if sys.platform.startswith("linux"):
                from Xlib import display
                d = display.Display()
                s = d.screen()
                size = (s.width_in_pixels, s.height_in_pixels)
                d.close()
                return size
            if sys.platform == "win32":
                import ctypes
                u = ctypes.windll.user32
                return (u.GetSystemMetrics(0), u.GetSystemMetrics(1))
        except Exception:  # noqa: BLE001
            pass
        return None

    def screenshot(self):
        from .vision import grab_screen
        return grab_screen()

    def origin(self):
        from .vision import screen_origin
        return screen_origin()

    def position(self):
        x, y = self.m.position
        return (int(x), int(y))

    def move(self, x, y):
        self.m.position = (int(x), int(y))

    def press(self, button):
        self.m.press(self._buttons[button])

    def release(self, button):
        self.m.release(self._buttons[button])

    def click(self, button, count=1):
        self.m.click(self._buttons[button], count)

    def scroll(self, dx, dy):
        self.m.scroll(dx, dy)

    def _key(self, name):
        K = self._keyboard.Key
        special = {"ctrl": K.ctrl, "shift": K.shift, "alt": K.alt, "super": K.cmd, "esc": K.esc,
                   "print_screen": K.print_screen}
        if name in special:
            return special[name]
        if len(name) > 1:
            key = getattr(K, name, None)
            if key is None:
                raise DriverError(f"клавиша «{name}» не поддерживается на этой системе")
            return key
        return self._keyboard.KeyCode.from_char(name)

    def key_down(self, key):
        self.k.press(self._key(key))

    def key_up(self, key):
        self.k.release(self._key(key))

    def type_char(self, ch):
        self.k.type(ch)
