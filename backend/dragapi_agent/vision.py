"""Компьютерное зрение агента: снимок экрана, поиск картинки, цвет пикселя, распознавание текста.

Библиотеки (numpy, opencv-python-headless, mss) подключаются при первом использовании: агент без них
работает, только блоки «Зрение» сообщат, что поставить. OCR — программа tesseract
(Linux: sudo apt install tesseract-ocr tesseract-ocr-rus; Windows: установщик UB Mannheim).
Путь к ней можно задать переменной DRAGAPI_TESSERACT.
"""

from __future__ import annotations

import base64
import os
import re
import shutil
import subprocess

MAX_TEMPLATE_BYTES = 2_000_000
DATA_URL_RE = re.compile(r"^data:image/(png|jpeg|jpg|webp|bmp);base64,([A-Za-z0-9+/=\s]+)$")
HEX_RE = re.compile(r"^#?([0-9a-fA-F]{6})$")


class VisionError(RuntimeError):
    pass


def _cv():
    try:
        import cv2
        import numpy as np
    except ImportError:
        raise VisionError("для блоков «Зрение» поставьте на агенте: pip install numpy opencv-python-headless mss") from None
    return cv2, np


# ---------- снимок экрана ----------

def grab_screen(region: tuple[int, int, int, int] | None = None):
    """Снимок всего экрана (все мониторы) или области (x, y, w, h) — массив BGR."""
    _, np = _cv()
    try:
        import mss
    except ImportError:
        raise VisionError("для снимков экрана поставьте на агенте: pip install mss") from None
    try:
        with mss.mss() as s:
            full = s.monitors[0]
            if region:
                x, y, w, h = region
                box = {"left": int(x), "top": int(y), "width": max(1, int(w)), "height": max(1, int(h))}
            else:
                box = full
            shot = s.grab(box)
    except Exception as e:  # noqa: BLE001 — нет X-сервера, Wayland и т.п.
        raise VisionError(f"не удалось снять экран: {e}") from None
    img = np.frombuffer(shot.bgra, dtype=np.uint8).reshape(shot.height, shot.width, 4)
    return np.ascontiguousarray(img[:, :, :3])


def screen_origin() -> tuple[int, int]:
    """Левый верхний угол всего экрана (на Windows второй монитор бывает с отрицательными координатами)."""
    try:
        import mss
        with mss.mss() as s:
            m = s.monitors[0]
            return int(m["left"]), int(m["top"])
    except Exception:  # noqa: BLE001
        return 0, 0


def crop(img, region):
    """Область (x, y, w, h) снимка всего экрана; смещение — чтобы вернуть координаты экрана."""
    if not region:
        return img, (0, 0)
    x, y, w, h = (int(v) for v in region)
    H, W = img.shape[:2]
    x0, y0 = max(0, min(x, W)), max(0, min(y, H))
    x1, y1 = max(x0, min(x + w, W)), max(y0, min(y + h, H))
    if x1 - x0 < 1 or y1 - y0 < 1:
        raise VisionError(f"область {x},{y} {w}×{h} за пределами экрана {W}×{H}")
    return img[y0:y1, x0:x1], (x0, y0)


def encode_png(img) -> str:
    cv2, _ = _cv()
    ok, buf = cv2.imencode(".png", img)
    if not ok:
        raise VisionError("не удалось сжать снимок")
    return "data:image/png;base64," + base64.b64encode(buf.tobytes()).decode("ascii")


def encode_jpeg(img, quality: int = 80) -> str:
    cv2, _ = _cv()
    ok, buf = cv2.imencode(".jpg", img, [cv2.IMWRITE_JPEG_QUALITY, quality])
    if not ok:
        raise VisionError("не удалось сжать снимок")
    return "data:image/jpeg;base64," + base64.b64encode(buf.tobytes()).decode("ascii")


_template_cache: dict[int, object] = {}


def decode_image(data_url: str):
    """data:image/png;base64,… → массив BGR (кэш — один сценарий ищет одну картинку много раз)."""
    key = hash(data_url)
    if key in _template_cache:
        return _template_cache[key]
    m = DATA_URL_RE.match(str(data_url or "").strip())
    if not m:
        raise VisionError("картинка-образец не задана")
    raw = base64.b64decode(m.group(2))
    if len(raw) > MAX_TEMPLATE_BYTES:
        raise VisionError("картинка-образец больше 2 МБ")
    cv2, np = _cv()
    img = cv2.imdecode(np.frombuffer(raw, dtype=np.uint8), cv2.IMREAD_COLOR)
    if img is None:
        raise VisionError("не удалось прочитать картинку-образец")
    if len(_template_cache) > 32:
        _template_cache.clear()
    _template_cache[key] = img
    return img


# ---------- поиск картинки ----------

def _match(screen, template, grayscale: bool):
    """Карта сходства 0…1 для каждой позиции образца.

    Обычно — нормированная корреляция (не зависит от яркости). У однотонного образца она не
    определена — для него сходство = 1 − среднеквадратичная разница цвета / 255."""
    cv2, np = _cv()
    a, b = screen, template
    if grayscale:
        a, b = cv2.cvtColor(a, cv2.COLOR_BGR2GRAY), cv2.cvtColor(b, cv2.COLOR_BGR2GRAY)
    if float(b.reshape(-1, b.shape[2] if b.ndim == 3 else 1).std(axis=0).max()) < 1.0:   # однотонный по каждому каналу
        sq = cv2.matchTemplate(a.astype(np.float32), b.astype(np.float32), cv2.TM_SQDIFF)
        return 1.0 - np.sqrt(np.maximum(sq, 0) / b.size) / 255.0
    return np.nan_to_num(cv2.matchTemplate(a, b, cv2.TM_CCOEFF_NORMED), nan=0.0)


def find_template(screen, template, *, grayscale: bool = False) -> dict:
    """Лучшее совпадение образца на снимке: {score, left, top, w, h, x, y} (x, y — центр)."""
    cv2, _ = _cv()
    th, tw = template.shape[:2]
    sh, sw = screen.shape[:2]
    if th > sh or tw > sw:
        return {"score": 0.0, "left": 0, "top": 0, "w": tw, "h": th, "x": 0, "y": 0}
    res = _match(screen, template, grayscale)
    _, score, _, (left, top) = cv2.minMaxLoc(res)
    return {"score": round(float(score), 4), "left": int(left), "top": int(top), "w": int(tw), "h": int(th),
            "x": int(left + tw // 2), "y": int(top + th // 2)}


def find_all(screen, template, confidence: float, *, grayscale: bool = False, limit: int = 100) -> list[dict]:
    """Все непересекающиеся совпадения не хуже confidence — по убыванию сходства."""
    cv2, np = _cv()
    th, tw = template.shape[:2]
    if th > screen.shape[0] or tw > screen.shape[1]:
        return []
    res = _match(screen, template, grayscale).copy()
    out = []
    for _ in range(limit):
        _, score, _, (left, top) = cv2.minMaxLoc(res)
        if score < confidence:
            break
        out.append({"score": round(float(score), 4), "left": int(left), "top": int(top), "w": int(tw), "h": int(th),
                    "x": int(left + tw // 2), "y": int(top + th // 2)})
        res[max(0, top - th // 2):top + th // 2 + 1, max(0, left - tw // 2):left + tw // 2 + 1] = -1
    return out


# ---------- цвет ----------

def parse_color(text: str) -> tuple[int, int, int]:
    m = HEX_RE.match(str(text or "").strip())
    if not m:
        raise VisionError(f"цвет в формате #RRGGBB, получено «{text}»")
    v = m.group(1)
    return int(v[0:2], 16), int(v[2:4], 16), int(v[4:6], 16)


def pixel_rgb(img, x: int, y: int) -> tuple[int, int, int]:
    h, w = img.shape[:2]
    if not (0 <= x < w and 0 <= y < h):
        raise VisionError(f"точка {x},{y} за пределами экрана {w}×{h}")
    b, g, r = (int(c) for c in img[y, x][:3])
    return r, g, b


def color_info(rgb) -> dict:
    r, g, b = rgb
    return {"r": r, "g": g, "b": b, "hex": f"#{r:02x}{g:02x}{b:02x}"}


def color_distance(a, b) -> int:
    """Наибольшее расхождение по каналу (0 — тот же цвет, 255 — противоположный)."""
    return max(abs(int(x) - int(y)) for x, y in zip(a, b))


# ---------- распознавание текста ----------

def tesseract_path() -> str | None:
    return os.environ.get("DRAGAPI_TESSERACT") or shutil.which("tesseract")


def ocr(img, lang: str = "rus+eng", *, digits: bool = False) -> str:
    exe = tesseract_path()
    if not exe:
        raise VisionError("для распознавания текста установите tesseract: Linux — sudo apt install tesseract-ocr "
                          "tesseract-ocr-rus; Windows — установщик tesseract (UB Mannheim) и путь в DRAGAPI_TESSERACT")
    cv2, _ = _cv()
    gray = cv2.cvtColor(img, cv2.COLOR_BGR2GRAY)
    if gray.shape[0] < 60:   # мелкий экранный шрифт распознаётся лучше в увеличенном виде
        gray = cv2.resize(gray, None, fx=3, fy=3, interpolation=cv2.INTER_CUBIC)
    elif gray.shape[0] < 400:
        gray = cv2.resize(gray, None, fx=2, fy=2, interpolation=cv2.INTER_CUBIC)
    if gray.mean() < 110:    # светлый текст на тёмном фоне → тёмный на светлом
        gray = 255 - gray
    ok, buf = cv2.imencode(".png", gray)
    args = [exe, "stdin", "stdout", "-l", lang, "--psm", "6"]
    if digits:
        args += ["-c", "tessedit_char_whitelist=0123456789.,-+:%"]
    try:
        r = subprocess.run(args, input=buf.tobytes(), capture_output=True, timeout=30)
    except subprocess.TimeoutExpired:
        raise VisionError("распознавание текста дольше 30 с — уменьшите область") from None
    if r.returncode != 0:
        err = r.stderr.decode("utf-8", "replace").strip().splitlines()
        msg = next((line for line in err if "Failed loading language" in line or "Error" in line), err[-1] if err else "")
        if "Failed loading language" in msg:
            raise VisionError(f"нет языка распознавания: {msg}. Linux: sudo apt install tesseract-ocr-rus")
        raise VisionError(f"tesseract: {msg or 'ошибка'}")
    text = r.stdout.decode("utf-8", "replace")
    return "\n".join(line.strip() for line in text.splitlines() if line.strip())
