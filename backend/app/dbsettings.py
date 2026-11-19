"""База данных API-проекта: встроенная SQLite или PostgreSQL (своя или уже существующая).

Настройки проекта (поле database):

    engine        sqlite | postgres
    url_env       переменная окружения проекта со строкой подключения (секрет), по умолчанию DATABASE_URL
    schema        manage   — платформа создаёт таблицы по моделям и мигрирует их (как с SQLite);
                  existing — существующая база: схема не меняется, модели должны совпадать с таблицами
    tests_use_db  тестовые запросы из редактора — в эту же базу (по умолчанию в отдельную SQLite-копию)
"""

from __future__ import annotations

import re
import uuid
from dataclasses import asdict, dataclass

ENGINES = ("sqlite", "postgres")
SCHEMAS = ("manage", "existing")
ENV_RE = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")
IDENT_RE = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")
LOCAL_HOSTS = ("localhost", "127.0.0.1", "::1")


@dataclass
class DatabaseSettings:
    engine: str = "sqlite"
    url_env: str = "DATABASE_URL"
    schema: str = "manage"
    tests_use_db: bool = False

    @property
    def postgres(self) -> bool:
        return self.engine == "postgres"

    def as_dict(self) -> dict:
        return asdict(self)

    @classmethod
    def parse(cls, raw, env) -> tuple["DatabaseSettings", list[str], list[str]]:
        """-> (настройки, ошибки, предупреждения). env — список EnvVar проекта."""
        raw = raw if isinstance(raw, dict) else {}
        errors, warnings = [], []
        s = cls(engine=str(raw.get("engine") or "sqlite"), url_env=str(raw.get("url_env") or "DATABASE_URL"),
                schema=str(raw.get("schema") or "manage"), tests_use_db=bool(raw.get("tests_use_db")))
        if s.engine not in ENGINES:
            errors.append(f"база данных: неизвестный тип «{s.engine}» (sqlite или postgres)")
        if s.schema not in SCHEMAS:
            errors.append(f"база данных: неизвестный режим схемы «{s.schema}»")
        if not s.postgres:
            return s, errors, warnings
        if not ENV_RE.match(s.url_env):
            errors.append(f"база данных: «{s.url_env}» — недопустимое имя переменной окружения")
            return s, errors, warnings
        var = next((e for e in env if e.key == s.url_env), None)
        if var is None:
            errors.append(f"база данных: заведите переменную окружения {s.url_env} со строкой подключения "
                          "postgresql://пользователь:пароль@сервер:5432/база (Настройки проекта → Переменные)")
        else:
            if not var.secret:
                warnings.append(f"база данных: в {s.url_env} обычно есть пароль — отметьте переменную секретом, "
                                "иначе она попадёт в экспорт открытым текстом")
            if var.value and not re.match(r"^postgres(ql)?(\+psycopg)?://", var.value):
                errors.append(f"база данных: {s.url_env} должна начинаться с postgresql:// "
                              "(например, postgresql://app:пароль@db.example.com:5432/shop)")
        return s, errors, warnings


def url_host(url: str) -> str | None:
    from sqlalchemy.engine import make_url
    try:
        return make_url(url).host
    except Exception:  # noqa: BLE001 — разбор чужой строки
        return None


def for_container(url: str) -> tuple[str, bool]:
    """Строка подключения изнутри Docker-контейнера: localhost этой машины -> host.docker.internal."""
    from sqlalchemy.engine import make_url
    try:
        u = make_url(url)
    except Exception:  # noqa: BLE001
        return url, False
    if u.host in LOCAL_HOSTS:
        return u.set(host="host.docker.internal").render_as_string(hide_password=False), True
    return url, False


def redact(url: str) -> str:
    from sqlalchemy.engine import make_url
    try:
        return make_url(url).render_as_string(hide_password=True)
    except Exception:  # noqa: BLE001
        return "(не разобрать строку подключения)"


# ---------- подключение и импорт моделей ----------


def _ftype(coltype) -> str:
    import sqlalchemy as sa
    if isinstance(coltype, sa.Boolean):
        return "bool"
    if isinstance(coltype, sa.Integer):
        return "int"
    if isinstance(coltype, (sa.Float, sa.Numeric)):
        return "float"
    return "str"


ON_DELETE_FROM_DB = {"CASCADE": "cascade", "SET NULL": "set_null"}


def inspect_database(url: str) -> dict:
    """Проверка подключения и таблицы базы с моделями, которые можно из них сделать."""
    import sqlalchemy as sa
    from .runtime import SCHEMA_TABLE, normalize_db_url

    engine = sa.create_engine(normalize_db_url(url), connect_args={"connect_timeout": 10})
    try:
        with engine.connect() as conn:
            server = conn.execute(sa.text("SHOW server_version")).scalar()
            info = conn.execute(sa.text("SELECT current_database(), current_user")).first()
            insp = sa.inspect(conn)
            tables, skipped = [], []
            names = sorted(t for t in insp.get_table_names() if t != SCHEMA_TABLE)
            for name in names:
                cols = insp.get_columns(name)
                pk = insp.get_pk_constraint(name).get("constrained_columns") or []
                id_col = next((c for c in cols if c["name"] == "id"), None)
                if not IDENT_RE.match(name):
                    skipped.append({"table": name, "reason": "имя таблицы не подходит для модели"})
                    continue
                if id_col is None or pk != ["id"] or _ftype(id_col["type"]) != "int":
                    skipped.append({"table": name, "reason": "нет целочисленного первичного ключа id"})
                    continue
                fks = {}
                for fk in insp.get_foreign_keys(name):
                    if len(fk["constrained_columns"]) == 1 and fk.get("referred_columns") == ["id"]:
                        fks[fk["constrained_columns"][0]] = (fk["referred_table"],
                                                             (fk.get("options") or {}).get("ondelete"))
                fields, dropped = [], []
                for c in cols:
                    if c["name"] == "id":
                        continue
                    if not IDENT_RE.match(c["name"]):
                        dropped.append(c["name"])
                        continue
                    f = {"name": c["name"], "type": _ftype(c["type"]), "db_type": str(c["type"]).lower()}
                    if c["name"] in fks:
                        f.update(type="ref", ref=fks[c["name"]][0],
                                 on_delete=ON_DELETE_FROM_DB.get(str(fks[c["name"]][1] or "").upper(), "restrict"))
                    fields.append(f)
                try:
                    rows = conn.execute(sa.text(f'SELECT count(*) FROM "{name}"')).scalar()
                except sa.exc.DBAPIError:
                    conn.rollback()
                    rows = None
                tables.append({"name": name, "rows": rows, "fields": fields, "skipped_columns": dropped})
        return {"ok": True, "server": f"PostgreSQL {server}", "database": info[0], "user": info[1],
                "tables": tables, "skipped": skipped}
    finally:
        engine.dispose()


def models_from_tables(tables: list[dict], pick: list[str] | None = None) -> list[dict]:
    """Модели проекта из таблиц inspect_database (ссылки — только на выбранные таблицы)."""
    chosen = [t for t in tables if pick is None or t["name"] in pick]
    names = {t["name"] for t in chosen}
    out = []
    for t in chosen:
        fields = []
        for f in t["fields"]:
            spec = {"id": uuid.uuid4().hex[:8], "name": f["name"], "type": f["type"]}
            if f["type"] == "ref":
                if f["ref"] not in names:   # связанная таблица не выбрана — просто число
                    spec["type"] = "int"
                else:
                    spec.update(ref=f["ref"], on_delete=f.get("on_delete") or "restrict")
            fields.append(spec)
        out.append({"id": uuid.uuid4().hex[:8], "name": t["name"], "fields": fields})
    return out


def connection_error(e: Exception) -> str:
    """Понятный текст ошибки подключения (без пароля)."""
    text = str(getattr(e, "orig", None) or e).strip().splitlines()[0] if str(e).strip() else type(e).__name__
    low = text.lower()
    if "password authentication failed" in low:
        return "неверный пользователь или пароль"
    if "does not exist" in low and "database" in low:
        return f"база не найдена: {text}"
    if "connection refused" in low or "could not connect" in low or "timeout" in low or "timed out" in low:
        return f"сервер не отвечает (адрес, порт, firewall?): {text}"
    if "no module named 'psycopg'" in low:
        return "не установлен драйвер PostgreSQL: pip install 'psycopg[binary]'"
    return text[:300]
