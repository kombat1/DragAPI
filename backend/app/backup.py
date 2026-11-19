"""Резервная копия всех данных платформы одним файлом и восстановление из неё.

Копия — zip каталога данных (backend/data или DRAGAPI_DATA_DIR): проекты и история, базы превью, пользователи,
боты, агенты, сертификаты и ключ шифрования секретов (без него секреты не расшифровать). Не входят:
окружения библиотек проектов (envs — пересобираются кнопкой «Установить») и журналы firewall.
Базы SQLite копируются через sqlite3 backup — копия целостная, даже если платформа работает.

С паролем zip шифруется целиком (scrypt + Fernet) — файл .dragapi-backup.enc.

Восстановление не трогает работающую платформу: архив распаковывается рядом (<data>.restore) и
применяется при следующем запуске — текущие данные переезжают в <data>.before-restore-<время>.
"""

from __future__ import annotations

import base64
import io
import json
import os
import shutil
import sqlite3
import tempfile
import time
import zipfile
from pathlib import Path, PurePosixPath

FORMAT = "dragapi.backup"
VERSION = 1
MAGIC = b"DRAGAPI-BACKUP-ENC1\n"
SKIP_DIRS = {"envs", "fw"}
SKIP_SUFFIXES = (".sock", ".tmp", ".lock")
MAX_UNPACKED = 4 * 1024 ** 3     # 4 ГБ — защита от «zip-бомбы»


class BackupError(ValueError):
    pass


def restore_dir(data: Path) -> Path:
    return data.with_name(data.name + ".restore")


def _files(data: Path):
    for p in sorted(data.rglob("*")):
        rel = p.relative_to(data)
        if rel.parts[0] in SKIP_DIRS or p.suffix in SKIP_SUFFIXES or not p.is_file() or p.is_symlink():
            continue
        yield p, rel


def _is_sqlite(p: Path) -> bool:
    try:
        with open(p, "rb") as f:
            return f.read(16) == b"SQLite format 3\x00"
    except OSError:
        return False


def _sqlite_copy(src: Path) -> bytes:
    """Целостный снимок базы SQLite, даже если в неё сейчас пишут."""
    with tempfile.TemporaryDirectory() as tmp:
        dst = Path(tmp) / "copy.sqlite"
        source = sqlite3.connect(f"file:{src}?mode=ro", uri=True)
        target = sqlite3.connect(dst)
        try:
            source.backup(target)
        finally:
            target.close()
            source.close()
        return dst.read_bytes()


def summary(data: Path) -> dict:
    """Что войдёт в копию: число проектов, размер, есть ли ключ шифрования."""
    size, files = 0, 0
    for p, _ in _files(data):
        files += 1
        size += p.stat().st_size
    projects = len(list((data / "projects").glob("*.json"))) if (data / "projects").exists() else 0
    return {"files": files, "bytes": size, "projects": projects, "has_key": (data / "secret.key").exists(),
            "excluded": sorted(SKIP_DIRS), "data_dir": str(data)}


def create(data: Path, password: str | None = None, *, app_version: str = "") -> bytes:
    """Архив каталога данных (и зашифрованный, если задан пароль)."""
    buf = io.BytesIO()
    info = summary(data)
    manifest = {"format": FORMAT, "version": VERSION, "created": time.time(), "app_version": app_version,
                "projects": info["projects"], "files": info["files"], "bytes": info["bytes"],
                "excluded": info["excluded"]}
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as z:
        z.writestr("manifest.json", json.dumps(manifest, ensure_ascii=False, indent=1))
        for p, rel in _files(data):
            name = "data/" + rel.as_posix()
            try:
                if _is_sqlite(p):
                    z.writestr(name, _sqlite_copy(p))
                else:
                    z.write(p, name)
            except (OSError, sqlite3.Error) as e:
                raise BackupError(f"не удалось прочитать {rel}: {e}") from None
    raw = buf.getvalue()
    return _encrypt(raw, password) if password else raw


def _key(password: str, salt: bytes) -> bytes:
    import hashlib
    return base64.urlsafe_b64encode(hashlib.scrypt(password.encode(), salt=salt, n=2 ** 15, r=8, p=1,
                                                   maxmem=64 * 1024 * 1024, dklen=32))


def _encrypt(raw: bytes, password: str) -> bytes:
    from cryptography.fernet import Fernet
    salt = os.urandom(16)
    return MAGIC + salt + Fernet(_key(password, salt)).encrypt(raw)


def is_encrypted(blob: bytes) -> bool:
    return blob.startswith(MAGIC)


def _decrypt(blob: bytes, password: str | None) -> bytes:
    from cryptography.fernet import Fernet, InvalidToken
    if not password:
        raise BackupError("копия зашифрована — введите пароль")
    salt = blob[len(MAGIC):len(MAGIC) + 16]
    try:
        return Fernet(_key(password, salt)).decrypt(blob[len(MAGIC) + 16:])
    except InvalidToken:
        raise BackupError("неверный пароль (или файл повреждён)") from None


def inspect(blob: bytes, password: str | None = None) -> tuple[dict, zipfile.ZipFile]:
    """Проверить файл копии -> (manifest, zip). Ошибки — BackupError с понятным текстом."""
    raw = _decrypt(blob, password) if is_encrypted(blob) else blob
    try:
        z = zipfile.ZipFile(io.BytesIO(raw))
    except zipfile.BadZipFile:
        raise BackupError("это не резервная копия DragAPI (не zip)") from None
    try:
        manifest = json.loads(z.read("manifest.json"))
    except (KeyError, ValueError):
        raise BackupError("в архиве нет manifest.json — это не резервная копия DragAPI") from None
    if manifest.get("format") != FORMAT:
        raise BackupError("это не резервная копия DragAPI")
    if int(manifest.get("version") or 0) > VERSION:
        raise BackupError("копия сделана более новой версией платформы — обновите платформу")
    total = 0
    for item in z.infolist():
        path = PurePosixPath(item.filename)
        if item.filename == "manifest.json" or item.is_dir():
            continue
        if path.is_absolute() or ".." in path.parts or path.parts[0] != "data" or "\\" in item.filename:
            raise BackupError(f"подозрительный путь в архиве: {item.filename}")
        total += item.file_size
    if total > MAX_UNPACKED:
        raise BackupError("архив слишком большой после распаковки")
    return manifest, z


def stage_restore(data: Path, blob: bytes, password: str | None = None) -> dict:
    """Распаковать копию рядом с данными; применится при следующем запуске (apply_pending)."""
    manifest, z = inspect(blob, password)
    staging = restore_dir(data)
    tmp = staging.with_name(staging.name + ".partial")
    shutil.rmtree(tmp, ignore_errors=True)
    tmp.mkdir(parents=True)
    for item in z.infolist():
        if item.filename == "manifest.json" or item.is_dir():
            continue
        rel = PurePosixPath(item.filename).relative_to("data")
        target = tmp.joinpath(*rel.parts)
        target.parent.mkdir(parents=True, exist_ok=True)
        with z.open(item) as src, open(target, "wb") as dst:
            shutil.copyfileobj(src, dst)
    if (tmp / "secret.key").exists() and os.name != "nt":
        (tmp / "secret.key").chmod(0o600)
    (tmp / ".restore-manifest.json").write_text(json.dumps(manifest, ensure_ascii=False), "utf-8")
    shutil.rmtree(staging, ignore_errors=True)
    tmp.rename(staging)
    return manifest


def pending(data: Path) -> dict | None:
    m = restore_dir(data) / ".restore-manifest.json"
    try:
        return json.loads(m.read_text("utf-8"))
    except (OSError, ValueError):
        return None


def cancel_pending(data: Path) -> bool:
    staging = restore_dir(data)
    if not staging.exists():
        return False
    shutil.rmtree(staging, ignore_errors=True)
    return True


def apply_pending(data: Path) -> Path | None:
    """При запуске: подготовленная копия становится каталогом данных. -> куда отложены прежние данные."""
    staging = restore_dir(data)
    if not (staging / ".restore-manifest.json").exists():
        return None
    previous = data.with_name(f"{data.name}.before-restore-{time.strftime('%Y%m%d-%H%M%S')}")
    if data.exists():
        data.rename(previous)
    (staging / ".restore-manifest.json").unlink()
    staging.rename(data)
    # окружения библиотек не входят в копию — возьмём прежние, если проекты те же
    if (previous / "envs").exists() and not (data / "envs").exists():
        shutil.move(str(previous / "envs"), str(data / "envs"))
    return previous if previous.exists() else None


def previous_copies(data: Path) -> list[dict]:
    out = []
    for p in sorted(data.parent.glob(data.name + ".before-restore-*")):
        out.append({"path": str(p), "name": p.name})
    return out
