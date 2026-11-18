"""Имена клавиш и сочетаний: «ctrl+shift+t», «enter», «f5», «a». Общие для платформы и агента."""

from __future__ import annotations

MODIFIERS = ["ctrl", "shift", "alt", "super"]
SPECIAL = [
    "enter", "esc", "tab", "space", "backspace", "delete", "insert", "home", "end", "page_up", "page_down",
    "up", "down", "left", "right", "caps_lock", "print_screen", "menu", "pause", "num_lock", "scroll_lock",
    "alt_gr", *[f"f{i}" for i in range(1, 21)],
]
ALIASES = {
    "control": "ctrl", "ctl": "ctrl", "win": "super", "cmd": "super", "meta": "super", "option": "alt",
    "return": "enter", "escape": "esc", "del": "delete", "ins": "insert", "pgup": "page_up",
    "pgdn": "page_down", "pageup": "page_up", "pagedown": "page_down", "altgr": "alt_gr",
    "prtsc": "print_screen", "printscreen": "print_screen", "caps": "caps_lock", "bksp": "backspace",
    "plus": "+", "spacebar": "space", "arrowup": "up", "arrowdown": "down", "arrowleft": "left",
    "arrowright": "right",
}
_KNOWN = set(MODIFIERS) | set(SPECIAL)


class KeyError_(ValueError):
    pass


def normalize(name: str) -> str:
    raw = str(name).strip()
    low = raw.lower()
    low = ALIASES.get(low, low)
    if low in _KNOWN:
        return low
    if len(raw) == 1:
        return raw.lower() if raw.isalpha() else raw
    if len(low) == 1:
        return low
    raise KeyError_(f"неизвестная клавиша «{name}»")


def parse_combo(text: str) -> list[str]:
    """«Ctrl+Shift+T» → ['ctrl', 'shift', 't']. Модификаторы — перед основной клавишей."""
    text = str(text or "").strip()
    if not text:
        raise KeyError_("пустое сочетание клавиш")
    parts = text.split("+")
    keys: list[str] = []
    i = 0
    while i < len(parts):
        part = parts[i]
        if part == "" and i + 1 < len(parts) and parts[i + 1] == "":  # «ctrl++» — клавиша плюс
            keys.append("+")
            i += 2
            continue
        if part.strip() == "":
            raise KeyError_(f"некорректное сочетание «{text}»")
        keys.append(normalize(part))
        i += 1
    mains = [k for k in keys if k not in MODIFIERS]
    if len(mains) > 1:
        raise KeyError_(f"в сочетании «{text}» больше одной основной клавиши")
    if mains and keys[-1] in MODIFIERS:
        raise KeyError_(f"в сочетании «{text}» модификаторы должны идти перед основной клавишей")
    if len(set(keys)) != len(keys):
        raise KeyError_(f"в сочетании «{text}» клавиша повторяется")
    return keys


def format_combo(keys: list[str]) -> str:
    return "+".join(keys)
