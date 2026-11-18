"""Блоки «Зрение»: поиск картинки, цвет пикселя, распознавание текста — на искусственном экране."""

import os
import shutil
import threading
import time
from pathlib import Path

import cv2
import numpy as np
import pytest
from fastapi.testclient import TestClient

from app import main as platform_main, storage
from app.automation import compile_automation
from app.graph import GraphError
from dragapi_agent import vision
from dragapi_agent.driver import FakeDriver
from tests.test_automation import _connect_agent, _wait, chain, edge, node, run


def make_screen(w=800, h=600):
    """Серый «рабочий стол» со случайным шумом и зелёной кнопкой «OK» в (500, 300)."""
    rng = np.random.default_rng(1)
    img = rng.integers(90, 140, (h, w, 3), dtype=np.uint8)
    cv2.rectangle(img, (500, 300), (580, 340), (60, 180, 60), -1)
    cv2.putText(img, "OK", (518, 332), cv2.FONT_HERSHEY_SIMPLEX, 0.9, (255, 255, 255), 2)
    return img


def png(img) -> str:
    return vision.encode_png(img)


SCREEN = make_screen()
BUTTON = png(SCREEN[300:341, 500:581])           # образец — сама кнопка
OTHER = png(np.full((30, 30, 3), (0, 0, 255), np.uint8))   # красный квадрат — его на экране нет


def find_graph(image=BUTTON, **data):
    g = chain(node("s", "trigger", name="main", kind="manual"),
              node("f", "image_find", image=image, **{"wait": "0", **data}))
    g["nodes"] += [node("yes", "log", message="нашли {found['x']},{found['y']}"), node("no", "log", message="нет")]
    g["edges"] += [edge("f", "yes", "true"), edge("f", "no", "false")]
    return g


def logs(events):
    return [e["message"] for e in events if e.get("type") == "log"]


def test_find_and_click_center():
    d = FakeDriver()
    d.screen = SCREEN
    result, d, events = run(find_graph(action="click", dx="0", dy="0"), driver=d)
    assert result["status"] == "ok", result
    found = result["vars"]["found"]
    assert found["found"] and found["score"] > 0.99
    assert (found["x"], found["y"]) == (540, 320) and (found["left"], found["top"], found["w"], found["h"]) == (500, 300, 81, 41)
    assert ("click", "left", 1, 540, 320) in d.actions
    assert "нашли 540,320" in logs(events)


def test_region_offset_and_double_click_with_shift():
    d = FakeDriver()
    d.screen = SCREEN
    result, d, _ = run(find_graph(region={"x": 400, "y": 250, "w": 300, "h": 200}, action="double", dx="-100", dy="5"),
                       driver=d)
    assert result["vars"]["found"]["x"] == 540        # координаты — экрана, а не области
    assert ("click", "left", 2, 440, 325) in d.actions
    # область без кнопки — не нашли
    d = FakeDriver()
    d.screen = SCREEN
    result, d, events = run(find_graph(region={"x": 0, "y": 0, "w": 300, "h": 200}), driver=d)
    assert not result["vars"]["found"]["found"] and "нет" in logs(events)
    assert not [a for a in d.actions if a[0] == "click"]


def test_not_found_waits_then_no_branch():
    d = FakeDriver()
    d.screen = SCREEN
    t = time.monotonic()
    result, d, events = run(find_graph(image=OTHER, wait="0.6"), driver=d)
    assert 0.5 < time.monotonic() - t < 2
    assert d.shots >= 3                               # проверял несколько раз
    assert result["vars"]["found"]["found"] is False and "нет" in logs(events)
    assert any("не найдено: лучшее сходство" in m for m in logs(events))


def test_wait_until_appears_and_disappears():
    shots = {"n": 0}
    empty = make_screen()
    empty[300:341, 500:581] = 115

    def screen():                       # кнопка появляется с третьего снимка
        shots["n"] += 1
        return SCREEN if shots["n"] >= 3 else empty

    d = FakeDriver()
    d.screen = screen
    result, d, _ = run(find_graph(wait="5", action="none"), driver=d)
    assert result["vars"]["found"]["found"] and shots["n"] == 3

    shots["n"] = 0
    d.screen = lambda: (shots.__setitem__("n", shots["n"] + 1), SCREEN if shots["n"] < 3 else empty)[1]
    result, d, events = run(find_graph(mode="disappear", wait="5"), driver=d)
    assert shots["n"] == 3 and logs(events)[0] == "картинка исчезла" and logs(events)[1].startswith("нашли")
    assert not [a for a in d.actions if a[0] == "click"]          # «исчезнет» не кликает


def test_find_all_and_uniform_template():
    screen = SCREEN.copy()
    screen[100:141, 100:181] = SCREEN[300:341, 500:581]           # вторая такая же кнопка
    tpl = vision.decode_image(BUTTON)
    hits = vision.find_all(screen, tpl, 0.9)
    assert sorted((h["x"], h["y"]) for h in hits) == [(140, 120), (540, 320)]
    flat = np.full((20, 20, 3), 50, np.uint8)
    canvas = np.full((200, 200, 3), 200, np.uint8)
    canvas[50:70, 60:80] = 50
    m = vision.find_template(canvas, flat)                       # однотонный образец — не NaN
    assert m["score"] > 0.99 and (m["left"], m["top"]) == (60, 50)


def test_pixel_check():
    d = FakeDriver()
    d.screen = SCREEN
    g = chain(node("s", "trigger", name="main", kind="manual"),
              node("p", "pixel_check", x="505", y="305", color="#3cb43c", tolerance="5"))
    g["nodes"] += [node("yes", "log", message="зелёный {pixel['hex']}"), node("no", "log", message="другой")]
    g["edges"] += [edge("p", "yes", "true"), edge("p", "no", "false")]
    result, _, events = run(g, driver=d)
    assert result["vars"]["pixel"]["hex"] == "#3cb43c" and result["vars"]["pixel"]["match"]
    assert "зелёный #3cb43c" in logs(events)
    g["nodes"][1]["data"].update(color="#ff0000")
    result, _, events = run(g, driver=d)
    assert not result["vars"]["pixel"]["match"] and "другой" in logs(events)
    g["nodes"][1]["data"].update(x="5000")
    result, _, _ = run(g, driver=d)
    assert result["status"] == "error" and "за пределами экрана" in result["message"]


def test_compile_vision_rules():
    base = node("s", "trigger", name="main", kind="manual")
    with pytest.raises(GraphError, match="нет образца"):
        compile_automation(chain(base, node("f", "image_find", image="")))
    with pytest.raises(GraphError, match="PNG или JPEG"):
        compile_automation(chain(base, node("f", "image_find", image="http://x/y.png")))
    with pytest.raises(GraphError, match="ширина и высота больше 0"):
        compile_automation(chain(base, node("f", "image_find", image=BUTTON, region={"x": 1, "y": 1, "w": 0, "h": 5})))
    with pytest.raises(GraphError, match="выделите область"):
        compile_automation(chain(base, node("o", "ocr")))
    with pytest.raises(GraphError, match="#RRGGBB"):
        compile_automation(chain(base, node("p", "pixel_check", color="зелёный")))
    plan = compile_automation(chain(base, node("p", "pixel_check", color="22C55E"))).scenario("main").plan()
    assert plan["steps"][0]["color"] == "#22c55e" and plan["steps"][0]["op"] == "pixel_check"


def test_missing_screen_is_step_error():
    result, _, _ = run(find_graph())
    assert result["status"] == "error" and "нет экрана" in result["message"]


# --- OCR: нужен tesseract (в системе или DRAGAPI_TESSERACT) ---

needs_tesseract = pytest.mark.skipif(not vision.tesseract_path(), reason="tesseract не установлен")


@needs_tesseract
def test_ocr_reads_text_and_digits():
    img = np.full((120, 700, 3), 255, np.uint8)
    cv2.putText(img, "Balance:", (20, 60), cv2.FONT_HERSHEY_SIMPLEX, 1.4, (0, 0, 0), 3)
    cv2.putText(img, "1250", (420, 60), cv2.FONT_HERSHEY_SIMPLEX, 1.4, (0, 0, 0), 3)
    d = FakeDriver()
    d.screen = img
    g = chain(node("s", "trigger", name="main", kind="manual"),
              node("o", "ocr", region={"x": 0, "y": 0, "w": 700, "h": 120}, lang="eng", target="text"),
              node("c", "set_vars", assignments=[{"target": "rich", "expr": "contains(text, '1250')"}]))
    result, _, _ = run(g, driver=d)
    assert result["status"] == "ok", result
    assert "Balance" in result["vars"]["text"] and result["vars"]["rich"] is True
    g["nodes"][1]["data"].update(region={"x": 400, "y": 0, "w": 300, "h": 120}, digits=True)
    result, _, _ = run(g, driver=d)
    assert result["vars"]["text"] == "1250"


def test_ocr_without_tesseract(monkeypatch):
    monkeypatch.setattr(vision, "tesseract_path", lambda: None)
    d = FakeDriver()
    d.screen = SCREEN
    g = chain(node("s", "trigger", name="main", kind="manual"),
              node("o", "ocr", region={"x": 0, "y": 0, "w": 100, "h": 50}))
    result, _, _ = run(g, driver=d)
    assert result["status"] == "error" and "установите tesseract" in result["message"]


# --- через платформу: снимок экрана и «Проверить» ---

def test_screenshot_and_vision_test_via_platform(tmp_path, monkeypatch):
    monkeypatch.setattr(storage, "DATA_DIR", tmp_path / "projects")
    monkeypatch.setattr(storage, "DB_DIR", tmp_path / "db")
    with TestClient(platform_main.app, headers={"X-DragAPI": "1"}) as client:
        created = client.post("/api/agents", json={"name": "ПК"}).json()
        d = FakeDriver()
        d.screen = SCREEN
        agent, ctx = _connect_agent(client, created["token"], d)
        try:
            agent_id = created["agent"]["id"]
            _wait(lambda: client.get("/api/agents").json()[0]["online"])
            pid = client.post("/api/projects", json={"name": "Зрение", "kind": "automation"}).json()["id"]
            r = client.post(f"/api/projects/{pid}/automation/screenshot", json={"agent": agent_id})
            assert r.status_code == 200, r.text
            req = r.json()["id"]
            shot = _wait(lambda: (x := client.get(f"/api/projects/{pid}/automation/request/{req}").json()).get("done") and x)
            assert (shot["w"], shot["h"]) == (800, 600) and shot["data"].startswith("data:image/png;base64,")
            assert vision.decode_image(shot["data"]).shape == (600, 800, 3)
            # снимок отдаётся один раз
            assert "data" not in client.get(f"/api/projects/{pid}/automation/request/{req}").json()

            graph = {**find_graph(action="click"), "automation": {"agent": agent_id}}
            r = client.post(f"/api/projects/{pid}/automation/vision-test", json={"agent": agent_id, "node": "f", "graph": graph})
            assert r.status_code == 200, r.text
            res = _wait(lambda: (x := client.get(f"/api/projects/{pid}/automation/request/{r.json()['id']}").json()).get("done") and x)
            assert res["status"] == "ok" and res["vars"]["found"]["x"] == 540 and "найдено в 540,320" in res["message"]
            assert not [a for a in d.actions if a[0] == "click"]       # проверка не кликает

            r = client.post(f"/api/projects/{pid}/automation/vision-test", json={"agent": agent_id, "node": "nope", "graph": graph})
            assert r.status_code == 400
            # ошибка в другом блоке проверке не мешает; ошибка в самом блоке — 400 с описанием
            broken = {**graph, "nodes": graph["nodes"] + [node("bad", "ocr")]}
            r = client.post(f"/api/projects/{pid}/automation/vision-test", json={"agent": agent_id, "node": "f", "graph": broken})
            assert r.status_code == 200, r.text
            res = _wait(lambda: (x := client.get(f"/api/projects/{pid}/automation/request/{r.json()['id']}").json()).get("done") and x)
            assert res["status"] == "ok" and res["vars"]["found"]["found"]
            r = client.post(f"/api/projects/{pid}/automation/vision-test", json={"agent": agent_id, "node": "bad", "graph": broken})
            assert r.status_code == 400 and "выделите область" in r.text
        finally:
            ctx.__exit__(None, None, None)


def test_screen_origin_offset():
    """Экран начинается не с 0,0 (монитор слева от основного): координаты — экранные."""
    class LeftMonitorDriver(FakeDriver):
        def origin(self):
            return (-1920, 0)

    d = LeftMonitorDriver()
    d.screen = SCREEN
    result, d, _ = run(find_graph(action="click"), driver=d)
    assert (result["vars"]["found"]["x"], result["vars"]["found"]["y"]) == (540 - 1920, 320)
    assert ("click", "left", 1, 540 - 1920, 320) in d.actions
    d = LeftMonitorDriver()
    d.screen = SCREEN
    result, _, _ = run(find_graph(region={"x": -1920 + 400, "y": 250, "w": 300, "h": 200}), driver=d)
    assert result["vars"]["found"]["found"] and result["vars"]["found"]["x"] == 540 - 1920
