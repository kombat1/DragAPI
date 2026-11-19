"""Окна рабочего стола: список, активировать, свернуть / развернуть / восстановить, переместить, закрыть.

Linux — X11 и оконный менеджер со стандартом EWMH (GNOME, KDE, XFCE, Cinnamon, MATE, Openbox …)
через python-xlib (ставится вместе с pynput). Windows — WinAPI через ctypes. macOS не поддерживается.

Окно — словарь {id, title, process, pid, x, y, w, h, state, active}: x, y, w, h — клиентская область
(без рамки и заголовка) в координатах экрана, state — normal | minimized | maximized.
Список — сверху вниз: первое окно лежит поверх остальных.
"""

from __future__ import annotations

import os
import sys
import threading
import time


class WindowError(RuntimeError):
    pass


ACTIONS = ("activate", "minimize", "maximize", "restore", "move", "close")


def backend():
    if sys.platform.startswith("linux"):
        return X11Windows()
    if sys.platform == "win32":
        return Win32Windows()
    raise WindowError("блоки окон работают на Linux (X11) и Windows; на macOS пока нет — "
                      "используйте поиск картинки на экране")


def _process_name(pid: int | None) -> str:
    if not pid:
        return ""
    try:
        with open(f"/proc/{pid}/comm", encoding="utf-8") as f:
            return f.read().strip()
    except OSError:
        return ""


# ---------- Linux / X11 (EWMH) ----------

class X11Windows:
    _lock = threading.Lock()

    def __init__(self):
        if not os.environ.get("DISPLAY"):
            raise WindowError("нет графического сеанса X11 (переменная DISPLAY не задана)")
        try:
            from Xlib import X, display  # noqa: F401
        except ImportError:
            raise WindowError("для работы с окнами поставьте на агенте: pip install python-xlib") from None

    def _open(self):
        from Xlib import display
        try:
            return display.Display()
        except Exception as e:  # noqa: BLE001
            raise WindowError(f"нет связи с X-сервером: {e}") from None

    @staticmethod
    def _prop(d, win, name, kind=None):
        from Xlib import X
        try:
            atom = d.intern_atom(name)
            r = win.get_full_property(atom, kind if kind is not None else X.AnyPropertyType)
        except Exception:  # noqa: BLE001 — окно уже закрылось
            return None
        return r.value if r is not None else None

    def _title(self, d, win) -> str:
        raw = self._prop(d, win, "_NET_WM_NAME", d.intern_atom("UTF8_STRING"))
        if raw is None:
            raw = self._prop(d, win, "WM_NAME")
        if isinstance(raw, bytes):
            return raw.decode("utf-8", "replace")
        return str(raw or "")

    def _info(self, d, root, wid: int, active: int | None) -> dict | None:
        try:
            win = d.create_resource_object("window", wid)
            types = self._prop(d, win, "_NET_WM_WINDOW_TYPE") or []
            skip = {d.intern_atom(f"_NET_WM_WINDOW_TYPE_{t}") for t in ("DESKTOP", "DOCK", "SPLASH")}
            if any(t in skip for t in types):
                return None
            geo = win.get_geometry()
            pos = win.translate_coords(root, 0, 0)       # где начало экрана относительно окна
            states = set(self._prop(d, win, "_NET_WM_STATE") or [])
            wm_state = self._prop(d, win, "WM_STATE")
        except Exception:  # noqa: BLE001
            return None
        hidden = d.intern_atom("_NET_WM_STATE_HIDDEN") in states or (wm_state is not None and len(wm_state) and wm_state[0] == 3)
        maxed = {d.intern_atom("_NET_WM_STATE_MAXIMIZED_VERT"), d.intern_atom("_NET_WM_STATE_MAXIMIZED_HORZ")} <= states
        pid = (self._prop(d, win, "_NET_WM_PID") or [None])[0]
        return {"id": int(wid), "title": self._title(d, win), "process": _process_name(pid), "pid": pid,
                "x": -pos.x, "y": -pos.y, "w": geo.width, "h": geo.height,
                "state": "minimized" if hidden else "maximized" if maxed else "normal", "active": wid == active}

    def list(self) -> list[dict]:
        with self._lock:
            d = self._open()
            try:
                root = d.screen().root
                ids = self._prop(d, root, "_NET_CLIENT_LIST_STACKING") or self._prop(d, root, "_NET_CLIENT_LIST")
                if ids is None:
                    raise WindowError("оконный менеджер не сообщает список окон (нужен EWMH: GNOME, KDE, XFCE …)")
                active = (self._prop(d, root, "_NET_ACTIVE_WINDOW") or [None])[0]
                out = [self._info(d, root, wid, active) for wid in reversed(list(ids))]
                return [w for w in out if w is not None]
            finally:
                d.close()

    def get(self, wid: int) -> dict | None:
        with self._lock:
            d = self._open()
            try:
                root = d.screen().root
                ids = self._prop(d, root, "_NET_CLIENT_LIST") or []
                if int(wid) not in [int(i) for i in ids]:
                    return None
                active = (self._prop(d, root, "_NET_ACTIVE_WINDOW") or [None])[0]
                return self._info(d, root, int(wid), active)
            finally:
                d.close()

    def _send(self, d, win, name: str, data: list[int]):
        from Xlib import X, protocol
        root = d.screen().root
        ev = protocol.event.ClientMessage(window=win, client_type=d.intern_atom(name),
                                          data=(32, (data + [0] * 5)[:5]))
        root.send_event(ev, event_mask=X.SubstructureRedirectMask | X.SubstructureNotifyMask)

    def do(self, wid: int, action: str, x=None, y=None, w=None, h=None):
        self._do(wid, action, x, y, w, h)
        if action == "move" and (x is not None or y is not None):
            # менеджеры окон по-разному учитывают рамку: сверяем итог и один раз поправляем сдвиг
            want = lambda i: (x is None or i["x"] == int(x)) and (y is None or i["y"] == int(y))  # noqa: E731
            info = wait_state(self.get, wid, want, 1.0)
            if info is not None and not want(info):
                dx = 0 if x is None else info["x"] - int(x)
                dy = 0 if y is None else info["y"] - int(y)
                if abs(dx) < 200 and abs(dy) < 200:
                    self._do(wid, "move", None if x is None else int(x) - dx, None if y is None else int(y) - dy, w, h)
                    wait_state(self.get, wid, want, 1.0)

    def _do(self, wid: int, action: str, x=None, y=None, w=None, h=None):
        from Xlib import X
        with self._lock:
            d = self._open()
            try:
                win = d.create_resource_object("window", int(wid))
                maxv, maxh = d.intern_atom("_NET_WM_STATE_MAXIMIZED_VERT"), d.intern_atom("_NET_WM_STATE_MAXIMIZED_HORZ")
                if action == "activate":
                    self._send(d, win, "_NET_ACTIVE_WINDOW", [2, X.CurrentTime, 0])
                elif action == "minimize":
                    self._send(d, win, "WM_CHANGE_STATE", [3])            # IconicState
                elif action == "maximize":
                    self._send(d, win, "_NET_WM_STATE", [1, maxv, maxh, 2])
                elif action == "restore":
                    self._send(d, win, "_NET_WM_STATE", [0, maxv, maxh, 2])
                    self._send(d, win, "_NET_ACTIVE_WINDOW", [2, X.CurrentTime, 0])   # из свёрнутого — тоже
                elif action == "move":
                    self._send(d, win, "_NET_WM_STATE", [0, maxv, maxh, 2])
                    flags = 10                                              # StaticGravity: x, y — клиентской области
                    vals = []
                    for bit, v in enumerate((x, y, w, h)):
                        if v is not None:
                            flags |= 1 << (8 + bit)
                        vals.append(int(v or 0))
                    self._send(d, win, "_NET_MOVERESIZE_WINDOW", [flags, *vals])
                elif action == "close":
                    self._send(d, win, "_NET_CLOSE_WINDOW", [X.CurrentTime, 2])
                else:
                    raise WindowError(f"неизвестное действие с окном «{action}»")
                d.sync()
            finally:
                d.close()


# ---------- Windows ----------

class Win32Windows:
    def __init__(self):
        import ctypes
        from ctypes import wintypes
        self.ct, self.wt = ctypes, wintypes
        self.u = ctypes.windll.user32
        self.k = ctypes.windll.kernel32
        try:
            self.dwm = ctypes.windll.dwmapi
        except OSError:
            self.dwm = None
        try:   # координаты в настоящих пикселях — как у мыши и снимков экрана
            ctypes.windll.shcore.SetProcessDpiAwareness(2)
        except Exception:  # noqa: BLE001
            pass

    def _process(self, hwnd) -> tuple[int, str]:
        ct, wt = self.ct, self.wt
        pid = wt.DWORD()
        self.u.GetWindowThreadProcessId(hwnd, ct.byref(pid))
        name = ""
        h = self.k.OpenProcess(0x1000, False, pid.value)          # PROCESS_QUERY_LIMITED_INFORMATION
        if h:
            buf = ct.create_unicode_buffer(1024)
            size = wt.DWORD(1024)
            if self.k.QueryFullProcessImageNameW(h, 0, buf, ct.byref(size)):
                name = os.path.basename(buf.value)
            self.k.CloseHandle(h)
        return pid.value, name

    def _visible(self, hwnd) -> bool:
        ct, wt = self.ct, self.wt
        if not self.u.IsWindowVisible(hwnd) or self.u.GetWindowTextLengthW(hwnd) == 0:
            return False
        if self.u.GetWindowLongW(hwnd, -20) & 0x80:              # WS_EX_TOOLWINDOW
            return False
        if self.dwm is not None:                                 # «спрятанные» окна приложений Магазина
            cloaked = wt.DWORD()
            self.dwm.DwmGetWindowAttribute(hwnd, 14, ct.byref(cloaked), ct.sizeof(cloaked))
            if cloaked.value:
                return False
        return True

    def _info(self, hwnd) -> dict:
        ct, wt = self.ct, self.wt
        n = self.u.GetWindowTextLengthW(hwnd)
        buf = ct.create_unicode_buffer(n + 1)
        self.u.GetWindowTextW(hwnd, buf, n + 1)
        rect = wt.RECT()
        self.u.GetClientRect(hwnd, ct.byref(rect))
        origin = wt.POINT(0, 0)
        self.u.ClientToScreen(hwnd, ct.byref(origin))
        pid, name = self._process(hwnd)
        state = "minimized" if self.u.IsIconic(hwnd) else "maximized" if self.u.IsZoomed(hwnd) else "normal"
        return {"id": int(hwnd), "title": buf.value, "process": name, "pid": pid, "x": origin.x, "y": origin.y,
                "w": rect.right, "h": rect.bottom, "state": state, "active": hwnd == self.u.GetForegroundWindow()}

    def list(self) -> list[dict]:
        ct, wt = self.ct, self.wt
        found = []
        proc = ct.WINFUNCTYPE(wt.BOOL, wt.HWND, wt.LPARAM)

        def cb(hwnd, _):
            if self._visible(hwnd):
                found.append(hwnd)
            return True

        self.u.EnumWindows(proc(cb), 0)                          # уже сверху вниз
        return [self._info(h) for h in found]

    def get(self, wid: int) -> dict | None:
        hwnd = self.wt.HWND(int(wid))
        if not self.u.IsWindow(hwnd):
            return None
        return self._info(hwnd)

    def do(self, wid: int, action: str, x=None, y=None, w=None, h=None):
        ct, wt = self.ct, self.wt
        hwnd = wt.HWND(int(wid))
        if not self.u.IsWindow(hwnd):
            raise WindowError("окно закрыто")
        if action == "activate":
            if self.u.IsIconic(hwnd):
                self.u.ShowWindow(hwnd, 9)                       # SW_RESTORE
            # Windows не даёт фоновому процессу забрать фокус — короткое нажатие Alt снимает запрет
            self.u.keybd_event(0x12, 0, 0, 0)
            self.u.keybd_event(0x12, 0, 2, 0)
            self.u.SetForegroundWindow(hwnd)
        elif action == "minimize":
            self.u.ShowWindow(hwnd, 6)
        elif action == "maximize":
            self.u.ShowWindow(hwnd, 3)
        elif action == "restore":
            self.u.ShowWindow(hwnd, 9)
            self.u.SetForegroundWindow(hwnd)
        elif action == "move":
            if self.u.IsZoomed(hwnd) or self.u.IsIconic(hwnd):
                self.u.ShowWindow(hwnd, 9)
            cur = self._info(hwnd)
            outer = wt.RECT()
            self.u.GetWindowRect(hwnd, ct.byref(outer))
            left, top = cur["x"] - outer.left, cur["y"] - outer.top                 # рамка слева и заголовок
            extra_w = (outer.right - outer.left) - cur["w"]
            extra_h = (outer.bottom - outer.top) - cur["h"]
            nx, ny = (cur["x"] if x is None else int(x)) - left, (cur["y"] if y is None else int(y)) - top
            nw, nh = (cur["w"] if w is None else int(w)) + extra_w, (cur["h"] if h is None else int(h)) + extra_h
            self.u.SetWindowPos(hwnd, None, nx, ny, nw, nh, 0x0004 | 0x0010)    # NOZORDER | NOACTIVATE
        elif action == "close":
            self.u.PostMessageW(hwnd, 0x0010, 0, 0)               # WM_CLOSE
        else:
            raise WindowError(f"неизвестное действие с окном «{action}»")


# ---------- общее ----------

def matches(win: dict, title: str, how: str = "contains", process: str = "") -> bool:
    import re
    t = win.get("title") or ""
    if process and process.lower() not in (win.get("process") or "").lower():
        return False
    if not title:
        return bool(process)
    if how == "exact":
        return t == title
    if how == "regex":
        try:
            return re.search(title, t) is not None
        except re.error as e:
            raise WindowError(f"регулярное выражение заголовка: {e}") from None
    return title.lower() in t.lower()


def at_point(windows: list[dict], x: int, y: int) -> dict | None:
    """Верхнее несвёрнутое окно, в клиентской области которого точка."""
    for w in windows:
        if w.get("state") != "minimized" and w["x"] <= x < w["x"] + w["w"] and w["y"] <= y < w["y"] + w["h"]:
            return w
    return None


def wait_state(get, wid: int, check, timeout: float = 1.5) -> dict | None:
    """Оконный менеджер выполняет команды асинхронно — подождать, пока окно придёт в нужное состояние."""
    end = time.monotonic() + timeout
    info = get(wid)
    while info is not None and not check(info) and time.monotonic() < end:
        time.sleep(0.05)
        info = get(wid)
    return info
