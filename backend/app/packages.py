"""Перенос проектов: файл .dragapi.json (экспорт / импорт) и бот отдельной программой (zip).

В файл попадает всё, что нужно, чтобы собрать проект заново: блоки, связи, модели, переменные,
настройки бота / автоматизации / песочницы. Не попадают:
  • значения секретов (и токенов, даже если переменная не отмечена секретом) — только имена;
  • данные: пользователи бота, платежи, история запусков, база превью;
  • служебное: токен вебхуков, «кем включено», id проекта.
"""

from __future__ import annotations

import io
import json
import re
import time
import zipfile
from pathlib import Path

FORMAT = "dragapi.project"
VERSION = 1
KINDS = ("api", "automation", "bot")
MAX_BYTES = 5_000_000
KEYS = ("nodes", "edges", "models", "env", "sandbox", "libraries", "firewall", "tests", "viewport",
        "bot", "automation", "telegram", "frames", "database")
SECRET_NAME = re.compile(r"TOKEN|SECRET|PASSW|API_?KEY|PRIVATE|CREDENTIAL", re.I)


class PackageError(ValueError):
    pass


def token_keys(project: dict) -> set[str]:
    """Переменные, в которых по настройкам лежат токены (их значения не экспортируются никогда)."""
    bot, tg, db = project.get("bot") or {}, project.get("telegram") or {}, project.get("database") or {}
    names = {bot.get("token_env"), bot.get("test_token_env"), bot.get("payments_env"), tg.get("token_env")}
    if db.get("engine") == "postgres":   # строка подключения с паролем
        names.add(db.get("url_env") or "DATABASE_URL")
    return {str(n) for n in names if n}


def _clean_env(project: dict) -> tuple[list[dict], list[str]]:
    tokens = token_keys(project)
    out, need = [], []
    for e in project.get("env") or []:
        key = str(e.get("key") or "")
        secret = bool(e.get("secret")) or key in tokens or bool(SECRET_NAME.search(key))
        item = {k: v for k, v in e.items() if k not in ("value_enc", "has_value")}
        if secret:
            item["value"], item["secret"] = "", True
            need.append(key)
        out.append(item)
    return out, need


def _strip_ids(project: dict) -> dict:
    """Для шаблона «поделиться»: без Telegram-id администраторов и разрешённых чатов."""
    project = dict(project)
    if isinstance(project.get("bot"), dict):
        project["bot"] = {**project["bot"], "admins": []}
    if isinstance(project.get("telegram"), dict):
        project["telegram"] = {**project["telegram"], "chats": []}
    return project


def export_package(project: dict, *, strip_ids: bool = False, source: str = "draft", version: str | None = None) -> dict:
    kind = project.get("kind") or "api"
    body = {k: project[k] for k in KEYS if k in project and project[k] not in (None, {}, [])}
    body["env"], need = _clean_env(project)
    if isinstance(body.get("automation"), dict):
        body["automation"] = {k: v for k, v in body["automation"].items()
                              if k not in ("hook_token_hash", "has_hook_token", "enabled_by", "enabled")}
    if kind != "automation":
        body.pop("automation", None)
    if strip_ids:
        body = _strip_ids(body)
    return {"format": FORMAT, "version": VERSION, "exported": time.time(), "source": source,
            "published_version": version, "secrets": need,
            "project": {"name": project.get("name") or "Проект", "kind": kind, **body}}


def read_package(raw: bytes | str | dict) -> dict:
    """Разобрать и проверить файл экспорта → поля проекта (без id)."""
    if isinstance(raw, (bytes, str)):
        if len(raw) > MAX_BYTES:
            raise PackageError("файл слишком большой (больше 5 МБ)")
        try:
            raw = json.loads(raw)
        except (ValueError, UnicodeDecodeError):
            raise PackageError("это не JSON-файл DragAPI") from None
    if not isinstance(raw, dict) or raw.get("format") != FORMAT or not isinstance(raw.get("project"), dict):
        raise PackageError("это не файл экспорта DragAPI (нет format: dragapi.project)")
    if not isinstance(raw.get("version"), int) or raw["version"] > VERSION:
        raise PackageError("файл сделан более новой версией DragAPI — обновите платформу")
    p = raw["project"]
    kind = p.get("kind") or "api"
    if kind not in KINDS:
        raise PackageError(f"неизвестный тип проекта «{kind}»")
    out: dict = {"name": str(p.get("name") or "Импорт")[:120], "kind": kind}
    for k in KEYS:
        if k not in p:
            continue
        v = p[k]
        want = list if k in ("nodes", "edges", "models", "env", "tests", "frames") else dict
        if v is not None and not isinstance(v, want):
            raise PackageError(f"поле «{k}» повреждено")
        out[k] = v
    for n in out.get("nodes") or []:
        if not isinstance(n, dict) or not isinstance(n.get("id"), str) or not isinstance(n.get("type"), str):
            raise PackageError("повреждён список блоков")
    if any(not isinstance(f, dict) for f in out.get("frames") or []):
        raise PackageError("повреждён список рамок")
    for e in out.get("edges") or []:
        if not isinstance(e, dict) or not isinstance(e.get("source"), str) or not isinstance(e.get("target"), str):
            raise PackageError("повреждён список связей")
    env = []
    for e in out.get("env") or []:
        if not isinstance(e, dict) or not isinstance(e.get("key"), str):
            raise PackageError("повреждён список переменных")
        env.append({k: v for k, v in e.items() if k not in ("value_enc", "has_value")})
    out["env"] = env
    if kind == "automation":
        auto = out.get("automation") or {}
        out["automation"] = {k: v for k, v in auto.items()
                             if k not in ("hook_token_hash", "has_hook_token", "enabled_by")} | {"enabled": False}
    else:
        out.pop("automation", None)
    return out


def missing_secrets(project: dict) -> list[str]:
    return [e["key"] for e in project.get("env") or [] if e.get("secret") and not e.get("value")]


# ---------- бот отдельной программой ----------

BACKEND = Path(__file__).resolve().parent.parent
STANDALONE_REQUIREMENTS = ["httpx==0.28.1", "sqlalchemy==2.0.36", "cryptography==44.0.0", "openpyxl==3.1.5"]

RUN_BOT = r'''"""Бот, собранный в DragAPI, — отдельной программой.

    pip install -r requirements.txt
    cp .env.example .env        # впишите токен и другие секреты
    python run_bot.py

Данные бота (пользователи, платежи, файлы, отложенные сообщения) — в папке data/.
"""

import asyncio
import json
import os
import signal
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))
os.environ.setdefault("DRAGAPI_DATA_DIR", str(HERE / "data"))


def load_dotenv(path: Path):
    if not path.exists():
        return
    for line in path.read_text("utf-8").splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, value = line.partition("=")
        value = value.strip()
        if len(value) >= 2 and value[0] == value[-1] and value[0] in "'\"":
            value = value[1:-1]
        os.environ.setdefault(key.strip(), value)


load_dotenv(HERE / ".env")

from app import bots, packages, storage  # noqa: E402

PID = "bot"


def prepare() -> dict:
    project = packages.read_package((HERE / "bot.json").read_bytes())
    for e in project["env"]:          # переменные окружения переопределяют значения из файла
        if os.environ.get(e["key"]):
            e["value"] = os.environ[e["key"]]
    missing = packages.missing_secrets(project)
    if missing:
        sys.exit("Не заданы переменные: " + ", ".join(missing) + "\nВпишите их в файл .env (см. .env.example)")
    return storage.save(PID, project)


async def main():
    prepare()
    bots.manager.loop = asyncio.get_running_loop()
    try:
        bots.manager.publish(PID, "запуск отдельной программы")
    except ValueError:
        pass                          # эта версия уже опубликована
    try:
        bot = await bots.manager.start(PID)
    except Exception as e:  # noqa: BLE001
        sys.exit(f"Бот не запущен: {e}")
    print(f"Бот @{bot.username} работает. Ctrl+C — остановить.", flush=True)
    scheduler = asyncio.ensure_future(bots.manager.scheduler())
    stop = asyncio.Event()
    for sig in (signal.SIGINT, signal.SIGTERM):
        try:
            asyncio.get_running_loop().add_signal_handler(sig, stop.set)
        except NotImplementedError:   # Windows
            pass
    last = None
    try:
        while not stop.is_set():
            err = bot.error
            if err != last and err:
                print("Ошибка:", err, flush=True)
            last = err
            try:
                await asyncio.wait_for(stop.wait(), 2)
            except asyncio.TimeoutError:
                pass
    finally:
        scheduler.cancel()
        await bots.manager.stop(PID, persist=False)
        print("Остановлено.")


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        pass
'''

DOCKERFILE = """FROM python:3.12-slim
WORKDIR /bot
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt
COPY . .
VOLUME /bot/data
CMD ["python", "run_bot.py"]
"""


def _readme(name: str, need: list[str]) -> str:
    env = "\n".join(f"  {k}" for k in need) or "  (секретов нет)"
    return f"""# {name}

Telegram-бот, собранный в DragAPI, — отдельной программой, без платформы.

## Запуск

```bash
python3 -m venv .venv && . .venv/bin/activate
pip install -r requirements.txt
cp .env.example .env      # впишите значения
python run_bot.py
```

Нужны переменные:
{env}

## Docker

```bash
docker build -t my-bot .
docker run -d --restart=always --env-file .env -v $PWD/data:/bot/data my-bot
```

## Что внутри

- `bot.json` — сам бот (блоки и настройки, без секретов). Его же можно импортировать обратно в DragAPI.
- `app/`, `dragapi_agent/` — движок ботов DragAPI.
- `data/` — появится при запуске: пользователи, роли, платежи, файлы, отложенные сообщения.
  Сохраняйте её при переносе на другой сервер.

Блоки «Python-код» и «HTTP-запрос» выполняются в песочнице: на Linux с firejail — изолированно,
иначе — обычным подпроцессом. Блок «Запустить сценарий» в отдельной программе недоступен
(он работает только внутри платформы).
"""


def _source_files() -> list[tuple[Path, str]]:
    files = []
    for pkg in ("app", "dragapi_agent"):
        for f in sorted((BACKEND / pkg).rglob("*.py")):
            if "__pycache__" not in f.parts:
                files.append((f, f.relative_to(BACKEND).as_posix()))
    return files


def _put(z: zipfile.ZipFile, name: str, data: str):
    info = zipfile.ZipInfo(name, time.localtime()[:6])
    info.external_attr, info.compress_type = 0o644 << 16, zipfile.ZIP_DEFLATED   # иначе в zip права 600
    z.writestr(info, data)


def standalone_zip(package: dict, slug: str) -> bytes:
    p = package["project"]
    if p.get("kind") != "bot":
        raise PackageError("отдельной программой можно собрать только бота")
    need = package.get("secrets") or []
    env_example = "".join(f"{k}=\n" for k in need)
    for e in p.get("env") or []:
        if e.get("key") not in need:
            env_example += f"# {e['key']}={e.get('value', '')}   (уже есть в bot.json)\n"
    buf = io.BytesIO()
    root = f"{slug}/"
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as z:
        put = lambda name, data: _put(z, root + name, data)  # noqa: E731
        put("bot.json", json.dumps(package, ensure_ascii=False, indent=2))
        put("run_bot.py", RUN_BOT)
        put("requirements.txt", "\n".join(STANDALONE_REQUIREMENTS) + "\n")
        put(".env.example", env_example or "# секретов нет\n")
        put("Dockerfile", DOCKERFILE)
        put(".dockerignore", ".env\ndata/\n.venv/\n")
        put("README.md", _readme(p.get("name") or slug, need))
        for path, arc in _source_files():
            put(arc, path.read_text("utf-8"))
    return buf.getvalue()
