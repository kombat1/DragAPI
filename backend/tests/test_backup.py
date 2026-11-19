"""Резервная копия всех данных платформы и восстановление из неё."""

import io
import json
import sqlite3
import subprocess
import sys
import zipfile
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from app import backup, paths
from app.main import app as platform_app


def make_data(root: Path) -> Path:
    data = root / "data"
    (data / "projects" / "shop.versions").mkdir(parents=True)
    (data / "projects" / "shop.json").write_text(json.dumps({"id": "shop", "name": "Магазин"}), "utf-8")
    (data / "projects" / "shop.versions" / "1.json").write_text("{}", "utf-8")
    (data / "db").mkdir()
    db = sqlite3.connect(data / "db" / "shop.sqlite")
    db.execute("create table t (x int)")
    db.execute("insert into t values (42)")
    db.commit()
    db.close()
    (data / "secret.key").write_text("KEY", "utf-8")
    (data / "envs" / "shop" / "venv").mkdir(parents=True)
    (data / "envs" / "shop" / "venv" / "big.bin").write_bytes(b"x" * 1000)
    (data / "fw").mkdir()
    (data / "fw" / "log.json").write_text("[]", "utf-8")
    return data


def test_create_contains_data_but_not_envs(tmp_path):
    data = make_data(tmp_path)
    info = backup.summary(data)
    assert info["projects"] == 1 and info["has_key"] and info["excluded"] == ["envs", "fw"]
    z = zipfile.ZipFile(io.BytesIO(backup.create(data, app_version="0.4.0")))
    names = set(z.namelist())
    assert {"manifest.json", "data/projects/shop.json", "data/projects/shop.versions/1.json",
            "data/db/shop.sqlite", "data/secret.key"} <= names
    assert not any(n.startswith(("data/envs", "data/fw")) for n in names)
    manifest = json.loads(z.read("manifest.json"))
    assert manifest["format"] == "dragapi.backup" and manifest["projects"] == 1 and manifest["app_version"] == "0.4.0"
    restored = tmp_path / "check.sqlite"
    restored.write_bytes(z.read("data/db/shop.sqlite"))
    assert sqlite3.connect(restored).execute("select x from t").fetchone() == (42,)


def test_encrypted_backup(tmp_path):
    data = make_data(tmp_path)
    blob = backup.create(data, "очень-секретно")
    assert backup.is_encrypted(blob) and b"shop.json" not in blob and b"KEY" not in blob
    with pytest.raises(backup.BackupError, match="введите пароль"):
        backup.inspect(blob)
    with pytest.raises(backup.BackupError, match="неверный пароль"):
        backup.inspect(blob, "не тот")
    manifest, z = backup.inspect(blob, "очень-секретно")
    assert manifest["projects"] == 1 and "data/secret.key" in z.namelist()


@pytest.mark.parametrize("name", ["../evil.txt", "/etc/passwd", "data/../../evil", "other/x"])
def test_rejects_bad_archives(tmp_path, name):
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as z:
        z.writestr("manifest.json", json.dumps({"format": "dragapi.backup", "version": 1, "created": 0}))
        z.writestr(name, "x")
    with pytest.raises(backup.BackupError, match="подозрительный путь"):
        backup.inspect(buf.getvalue())
    with pytest.raises(backup.BackupError, match="не zip"):
        backup.inspect(b"hello")
    with pytest.raises(backup.BackupError, match="manifest"):
        backup.inspect(_zip({"data/x": "1"}))


def _zip(files):
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as z:
        for k, v in files.items():
            z.writestr(k, v)
    return buf.getvalue()


def test_restore_applies_on_next_start(tmp_path):
    data = make_data(tmp_path)
    blob = backup.create(data)
    # после копии данные меняются
    (data / "projects" / "shop.json").write_text(json.dumps({"id": "shop", "name": "Изменено"}), "utf-8")
    (data / "projects" / "new.json").write_text("{}", "utf-8")
    manifest = backup.stage_restore(data, blob)
    assert manifest["projects"] == 1 and backup.pending(data)["projects"] == 1
    assert json.loads((data / "projects" / "shop.json").read_text("utf-8"))["name"] == "Изменено"  # пока не тронуто
    previous = backup.apply_pending(data)
    assert json.loads((data / "projects" / "shop.json").read_text("utf-8"))["name"] == "Магазин"
    assert not (data / "projects" / "new.json").exists()
    assert (previous / "projects" / "new.json").exists()          # прежние данные отложены рядом
    assert (data / "envs" / "shop" / "venv" / "big.bin").exists()  # окружения библиотек переехали
    assert backup.pending(data) is None and backup.apply_pending(data) is None
    assert [p["name"] for p in backup.previous_copies(data)] == [previous.name]
    # отмена подготовленного восстановления
    backup.stage_restore(data, blob)
    assert backup.cancel_pending(data) and backup.pending(data) is None


@pytest.fixture
def platform(tmp_path, monkeypatch):
    data = make_data(tmp_path)
    monkeypatch.setattr(paths, "DATA", data)
    with TestClient(platform_app, headers={"X-DragAPI": "1"}) as c:
        yield c, data


def test_api_download_and_restore(platform):
    c, data = platform
    info = c.get("/api/backup").json()
    assert info["projects"] == 1 and info["pending"] is None
    r = c.post("/api/backup", json={})
    assert r.status_code == 200 and r.headers["content-disposition"].endswith('.zip"')
    plain = r.content
    assert c.post("/api/backup", json={"password": "short"}).status_code == 400
    enc = c.post("/api/backup", json={"password": "long-password"})
    assert enc.headers["content-disposition"].endswith('.dragapi-backup.enc"') and backup.is_encrypted(enc.content)

    r = c.post("/api/backup/restore", files={"file": ("b.enc", enc.content)}, data={"password": "wrong-pass"})
    assert r.status_code == 400 and "неверный пароль" in r.json()["detail"]
    r = c.post("/api/backup/restore", files={"file": ("b.enc", enc.content)}, data={"password": "long-password"})
    assert r.status_code == 200 and r.json()["manifest"]["projects"] == 1
    assert c.get("/api/backup").json()["pending"]["projects"] == 1
    assert c.delete("/api/backup/restore").json() == {"ok": True}
    r = c.post("/api/backup/restore", files={"file": ("b.zip", plain)})
    assert r.status_code == 200
    r = c.post("/api/backup/restore", files={"file": ("x.txt", b"not a backup")})
    assert r.status_code == 400


def test_backup_is_admin_only(tmp_path, monkeypatch):
    from app import auth
    monkeypatch.setattr(paths, "DATA", make_data(tmp_path))
    monkeypatch.delenv("DRAGAPI_AUTH", raising=False)
    auth.store.create_user("boss", "very-secret-1", "admin")
    auth.store.create_user("ann", "ann-password", "user")
    with TestClient(platform_app, headers={"X-DragAPI": "1"}) as c:
        assert c.get("/api/backup").status_code == 401
        c.post("/api/auth/login", json={"username": "ann", "password": "ann-password"})
        assert c.get("/api/backup").status_code == 403
        assert c.post("/api/backup", json={}).status_code == 403
        c.post("/api/auth/login", json={"username": "boss", "password": "very-secret-1"})
        assert c.get("/api/backup").status_code == 200


def test_cli_backup_and_restore(tmp_path):
    data = make_data(tmp_path)
    env = {"DRAGAPI_DATA_DIR": str(data), "BK_PASS": "cli-password", "PYTHONUTF8": "1",
           "PATH": __import__("os").environ.get("PATH", ""), "SYSTEMROOT": __import__("os").environ.get("SYSTEMROOT", "")}
    backend = Path(__file__).resolve().parent.parent
    out = tmp_path / "copy.enc"
    r = subprocess.run([sys.executable, "-m", "app", "backup", str(out), "--password-env", "BK_PASS"],
                       cwd=backend, env=env, capture_output=True, text=True, encoding="utf-8")
    assert r.returncode == 0, r.stderr
    assert backup.is_encrypted(out.read_bytes())
    r = subprocess.run([sys.executable, "-m", "app", "restore", str(out), "--password-env", "BK_PASS"],
                       cwd=backend, env=env, capture_output=True, text=True, encoding="utf-8")
    assert r.returncode == 0, r.stderr
    assert backup.pending(data)["projects"] == 1
    # следующий запуск (любой импорт app.paths) применяет восстановление
    r = subprocess.run([sys.executable, "-c", "import app.paths"], cwd=backend, env=env, capture_output=True,
                       text=True, encoding="utf-8")
    assert "восстановлены из резервной копии" in r.stderr and backup.pending(data) is None
