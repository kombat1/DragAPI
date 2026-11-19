"""Перенос проектов: экспорт в .dragapi.json, импорт, бот отдельной программой."""

import io
import json
import zipfile

import pytest
from fastapi.testclient import TestClient

from app import main as platform_main
from app import packages, storage
from tests.test_bots import graph_form


@pytest.fixture
def platform(tmp_path, monkeypatch):
    monkeypatch.setattr(storage, "DATA_DIR", tmp_path / "projects")
    monkeypatch.setattr(storage, "DB_DIR", tmp_path / "db")
    with TestClient(platform_main.app, headers={"X-DragAPI": "1"}) as client:
        yield client


def _bot(c):
    pid = c.post("/api/projects", json={"name": "Мой бот", "kind": "bot"}).json()["id"]
    g = {**graph_form(), "name": "Мой бот",
         "env": [{"key": "BOT_TOKEN", "value": "42:abc", "secret": False},      # токен, хоть и не отмечен секретом
                 {"key": "SHOP_API_KEY", "value": "k-123", "secret": False},  # по имени — секрет
                 {"key": "PASS", "value": "p", "secret": True},
                 {"key": "GREETING", "value": "Привет", "secret": False}],
         "bot": {"token_env": "BOT_TOKEN", "admins": ["111"], "roles": ["moderator"]}}
    assert c.put(f"/api/projects/{pid}", json=g).status_code == 200
    return pid


def test_export_hides_secrets_and_tokens(platform):
    c = platform
    pid = _bot(c)
    r = c.get(f"/api/projects/{pid}/package")
    assert r.status_code == 200
    assert "filename*=UTF-8''%D0%9C%D0%BE%D0%B9-%D0%B1%D0%BE%D1%82.dragapi.json" in r.headers["content-disposition"]
    text = r.text
    for leaked in ("42:abc", "k-123", "value_enc"):
        assert leaked not in text
    pkg = r.json()
    assert pkg["format"] == "dragapi.project" and pkg["project"]["kind"] == "bot"
    assert sorted(pkg["secrets"]) == ["BOT_TOKEN", "PASS", "SHOP_API_KEY"]
    env = {e["key"]: e for e in pkg["project"]["env"]}
    assert env["GREETING"]["value"] == "Привет" and env["BOT_TOKEN"]["secret"] is True
    assert pkg["project"]["bot"]["admins"] == ["111"]
    shared = c.get(f"/api/projects/{pid}/package", params={"strip_ids": True}).json()
    assert shared["project"]["bot"]["admins"] == [] and shared["project"]["bot"]["roles"] == ["moderator"]


def test_import_roundtrip(platform):
    c = platform
    pid = _bot(c)
    pkg = c.get(f"/api/projects/{pid}/package").json()
    r = c.post("/api/import", json={"package": pkg})
    assert r.status_code == 200, r.text
    out = r.json()
    new = out["project"]["id"]
    assert new != pid and out["project"]["name"] == "Мой бот" and out["project"]["kind"] == "bot"
    assert sorted(out["missing_secrets"]) == ["BOT_TOKEN", "PASS", "SHOP_API_KEY"] and out["issues"] == []
    a, b = storage.load(pid), storage.load(new)
    assert [n["id"] for n in a["nodes"]] == [n["id"] for n in b["nodes"]] and a["edges"] == b["edges"]
    # тест-чат работает сразу, без токена
    r = c.post(f"/api/projects/{new}/bot/emulator", json={"text": "/start"})
    assert r.status_code == 200, r.text
    # другое имя
    assert c.post("/api/import", json={"package": pkg, "name": "Копия"}).json()["project"]["name"] == "Копия"


def test_import_automation_comes_disabled(platform):
    c = platform
    pid = c.post("/api/projects", json={"name": "Кликер", "kind": "automation"}).json()["id"]
    pkg = c.get(f"/api/projects/{pid}/package").json()
    pkg["project"]["automation"] = {"agent": "local", "enabled": True, "hook_token_hash": "abc"}
    out = c.post("/api/import", json={"package": pkg}).json()
    auto = storage.load(out["project"]["id"])["automation"]
    assert auto["enabled"] is False and "hook_token_hash" not in auto


@pytest.mark.parametrize("bad, msg", [
    ({"hello": 1}, "не файл экспорта"),
    ({"format": "dragapi.project", "version": 99, "project": {}}, "более новой версией"),
    ({"format": "dragapi.project", "version": 1, "project": {"kind": "rocket"}}, "неизвестный тип"),
    ({"format": "dragapi.project", "version": 1, "project": {"kind": "bot", "nodes": [{"id": 1}]}}, "блоков"),
    ({"format": "dragapi.project", "version": 1, "project": {"kind": "api", "edges": "x"}}, "edges"),
])
def test_import_rejects_bad_files(platform, bad, msg):
    r = platform.post("/api/import", json={"package": bad})
    assert r.status_code == 400 and msg in r.json()["detail"]


def test_api_project_roundtrip(platform):
    c = platform
    pid = c.post("/api/projects", json={"name": "API", "kind": "api"}).json()["id"]
    pkg = c.get(f"/api/projects/{pid}/package").json()
    assert pkg["project"]["kind"] == "api" and "automation" not in pkg["project"]
    out = c.post("/api/import", json={"package": pkg}).json()
    assert storage.load(out["project"]["id"])["kind"] == "api"


def test_published_export_and_standalone_zip(platform):
    c = platform
    pid = _bot(c)
    assert c.get(f"/api/projects/{pid}/package", params={"source": "published"}).status_code == 409
    assert c.post(f"/api/projects/{pid}/bot/publish", json={"note": "v1"}).status_code == 200
    g = storage.load(pid)
    g["nodes"][0]["data"]["description"] = "черновик"
    c.put(f"/api/projects/{pid}", json=g)
    pub = c.get(f"/api/projects/{pid}/package", params={"source": "published"}).json()
    assert pub["source"] == "published" and pub["published_version"]
    assert pub["project"]["nodes"][0]["data"].get("description") != "черновик"

    r = c.get(f"/api/projects/{pid}/package/standalone")
    assert r.status_code == 200 and r.headers["content-type"] == "application/zip"
    z = zipfile.ZipFile(io.BytesIO(r.content))
    names = set(z.namelist())
    root = next(iter(names)).split("/")[0]
    for f in ("run_bot.py", "bot.json", "requirements.txt", ".env.example", "Dockerfile", "README.md",
              "app/bots.py", "app/packages.py", "dragapi_agent/runner.py"):
        assert f"{root}/{f}" in names, f
    assert not any("__pycache__" in n or "/data/" in n for n in names)
    bot = json.loads(z.read(f"{root}/bot.json"))
    assert bot["source"] == "published" and "42:abc" not in z.read(f"{root}/bot.json").decode()
    env_example = z.read(f"{root}/.env.example").decode()
    assert "BOT_TOKEN=\n" in env_example and "42:abc" not in env_example
    packages.read_package(z.read(f"{root}/bot.json"))   # тот же файл импортируется обратно

    api = c.post("/api/projects", json={"name": "API", "kind": "api"}).json()["id"]
    assert c.get(f"/api/projects/{api}/package/standalone").status_code == 400


FRAMES = [{"id": "frame-a", "type": "frame", "position": {"x": 0, "y": 0}, "width": 400, "height": 300,
           "data": {"title": "Оплата", "color": "#059669"}},
          {"id": "note-b", "type": "note", "position": {"x": 500, "y": 0}, "width": 220, "height": 140,
           "data": {"text": "Не забыть про возвраты"}}]


def test_frames_saved_exported_and_published(platform):
    """Рамки и заметки: сохраняются, переносятся файлом, попадают в публикацию — и не влияют на бота."""
    c = platform
    pid = _bot(c)
    g = c.get(f"/api/projects/{pid}").json()
    assert c.post(f"/api/projects/{pid}/bot/publish", json={}).status_code == 200
    # только оформление поменялось — публиковать нечего
    assert c.put(f"/api/projects/{pid}", json={**g, "frames": FRAMES}).status_code == 200
    assert c.get(f"/api/projects/{pid}").json()["frames"] == FRAMES
    st = c.get(f"/api/projects/{pid}/bot/status").json()
    assert not st["publish"]["draft_differs"]
    assert c.post("/api/validate", json={**g, "frames": FRAMES}).json()["ok"]
    # файл экспорта и импорт
    pkg = c.get(f"/api/projects/{pid}/package").json()
    assert pkg["project"]["frames"] == FRAMES
    new = c.post("/api/import", json={"package": pkg}).json()["project"]["id"]
    assert c.get(f"/api/projects/{new}").json()["frames"] == FRAMES
    # снимок публикации хранит рамки
    g2 = c.get(f"/api/projects/{pid}").json()
    g2["nodes"][0]["data"] = {**g2["nodes"][0]["data"], "command": "hello"}
    c.put(f"/api/projects/{pid}", json=g2)
    c.post(f"/api/projects/{pid}/bot/publish", json={})
    published = c.get(f"/api/projects/{pid}/package", params={"source": "published"}).json()
    assert published["project"]["frames"] == FRAMES


def test_import_rejects_broken_frames(platform):
    pkg = {"format": "dragapi.project", "version": 1, "project": {"name": "x", "kind": "api", "frames": ["oops"]}}
    r = platform.post("/api/import", json={"package": pkg})
    assert r.status_code == 400 and "рамок" in r.json()["detail"]
