"""Хранилище проектов: JSON-файлы в data/projects. История версий — в data/projects/<id>.versions/.

Значения секретных переменных окружения на диске только зашифрованы (см. vault.py).
"""

from __future__ import annotations

import json
import re
import shutil
import time
import uuid
from pathlib import Path

from . import vault
from .paths import DATA

DATA_DIR = DATA / "projects"
DB_DIR = DATA / "db"
EXAMPLES_DIR = Path(__file__).resolve().parent.parent / "examples"
ID_RE = re.compile(r"^[a-z0-9][a-z0-9-]{0,63}$")
MAX_VERSIONS = 30


class NotFound(Exception):
    pass


def _path(pid: str) -> Path:
    if not ID_RE.match(pid):
        raise NotFound(pid)
    return DATA_DIR / f"{pid}.json"


def list_projects() -> list[dict]:
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    out = []
    for p in sorted(DATA_DIR.glob("*.json")):
        try:
            data = json.loads(p.read_text("utf-8"))
        except (OSError, json.JSONDecodeError):
            continue
        out.append({"id": p.stem, "name": data.get("name", p.stem), "updated": data.get("updated", 0),
                    "kind": data.get("kind") or "api"})
    return sorted(out, key=lambda x: -x["updated"])


def load(pid: str) -> dict:
    path = _path(pid)
    if not path.exists():
        raise NotFound(pid)
    return json.loads(path.read_text("utf-8"))


def save(pid: str, project: dict) -> dict:
    path = _path(pid)
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    previous_env = []
    if path.exists():
        try:
            previous_env = json.loads(path.read_text("utf-8")).get("env") or []
        except (OSError, json.JSONDecodeError):
            pass
    project = {**project, "env": vault.seal_env(project.get("env"), previous_env)}
    if path.exists():  # предыдущую версию — в историю
        vdir = DATA_DIR / f"{pid}.versions"
        vdir.mkdir(exist_ok=True)
        (vdir / f"{int(time.time() * 1000)}.json").write_text(path.read_text("utf-8"), "utf-8")
        for old in sorted(vdir.glob("*.json"))[:-MAX_VERSIONS]:
            old.unlink()
    project = {**project, "id": pid, "updated": time.time()}
    tmp = path.with_suffix(".tmp")
    tmp.write_text(json.dumps(project, ensure_ascii=False, indent=2), "utf-8")
    tmp.replace(path)
    return project


def create(name: str, kind: str = "api") -> dict:
    slug = re.sub(r"[^a-z0-9]+", "-", name.lower()).strip("-")[:40] or "project"
    pid = f"{slug}-{uuid.uuid4().hex[:6]}"
    project = {"name": name, "kind": kind, "nodes": [], "edges": [], "models": [], "env": [],
               "sandbox": {}, "libraries": {"packages": [], "only_binary": True}}
    if kind == "automation":
        project["automation"] = {"agent": "local", "enabled": False}
        project["nodes"] = [{"id": "start", "type": "trigger", "position": {"x": 80, "y": 120},
                             "data": {"name": "main", "kind": "manual", "hotkey": "ctrl+alt+1",
                                      "every_seconds": "60", "at": "09:00", "weekdays": [], "params": []}}]
    return save(pid, project)


def delete(pid: str) -> None:
    path = _path(pid)
    if not path.exists():
        raise NotFound(pid)
    path.unlink()
    shutil.rmtree(DATA_DIR / f"{pid}.versions", ignore_errors=True)
    (DB_DIR / f"{pid}.sqlite").unlink(missing_ok=True)


def db_url(pid: str) -> str:
    """SQLite-база превью проекта."""
    _path(pid)
    DB_DIR.mkdir(parents=True, exist_ok=True)
    return f"sqlite:///{DB_DIR / f'{pid}.sqlite'}"


def versions(pid: str) -> list[dict]:
    _path(pid)
    vdir = DATA_DIR / f"{pid}.versions"
    if not vdir.exists():
        return []
    return [{"version": p.stem, "time": int(p.stem) / 1000} for p in sorted(vdir.glob("*.json"), reverse=True)]


def load_version(pid: str, version: str) -> dict:
    _path(pid)
    if not version.isdigit():
        raise NotFound(version)
    p = DATA_DIR / f"{pid}.versions" / f"{version}.json"
    if not p.exists():
        raise NotFound(version)
    return json.loads(p.read_text("utf-8"))


def migrate_secrets() -> int:
    """Зашифровать секреты, сохранённые открытым текстом (проекты и их история версий)."""
    if not DATA_DIR.exists():
        return 0
    changed = 0
    for path in list(DATA_DIR.glob("*.json")) + list(DATA_DIR.glob("*.versions/*.json")):
        try:
            data = json.loads(path.read_text("utf-8"))
        except (OSError, json.JSONDecodeError):
            continue
        if vault.needs_sealing(data.get("env")):
            data["env"] = vault.seal_env(data["env"])
            tmp = path.with_suffix(".tmp")
            tmp.write_text(json.dumps(data, ensure_ascii=False, indent=2), "utf-8")
            tmp.replace(path)
            changed += 1
    return changed


def seed_examples() -> int:
    """При первом запуске (проектов ещё нет) — скопировать примеры из backend/examples."""
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    if any(DATA_DIR.glob("*.json")) or not EXAMPLES_DIR.exists():
        return 0
    count = 0
    for src in EXAMPLES_DIR.glob("*.json"):
        project = json.loads(src.read_text("utf-8"))
        save(src.stem, project)
        count += 1
    return count
