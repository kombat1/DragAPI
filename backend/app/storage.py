"""Хранилище проектов: JSON-файлы в data/projects. История версий — в data/projects/<id>.versions/."""

from __future__ import annotations

import json
import re
import shutil
import time
import uuid
from pathlib import Path

DATA_DIR = Path(__file__).resolve().parent.parent / "data" / "projects"
DB_DIR = Path(__file__).resolve().parent.parent / "data" / "db"
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
        out.append({"id": p.stem, "name": data.get("name", p.stem), "updated": data.get("updated", 0)})
    return sorted(out, key=lambda x: -x["updated"])


def load(pid: str) -> dict:
    path = _path(pid)
    if not path.exists():
        raise NotFound(pid)
    return json.loads(path.read_text("utf-8"))


def save(pid: str, project: dict) -> dict:
    path = _path(pid)
    DATA_DIR.mkdir(parents=True, exist_ok=True)
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


def create(name: str) -> dict:
    slug = re.sub(r"[^a-z0-9]+", "-", name.lower()).strip("-")[:40] or "project"
    pid = f"{slug}-{uuid.uuid4().hex[:6]}"
    return save(pid, {"name": name, "nodes": [], "edges": [], "models": [], "env": [],
                     "sandbox": {}, "libraries": {"packages": [], "only_binary": True}})


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
