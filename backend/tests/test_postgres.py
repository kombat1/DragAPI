"""PostgreSQL для API-проектов.

Без сервера проверяются настройки, экспорт и безопасность. С сервером (переменная DRAGAPI_PG_TEST_URL —
строка подключения пользователя, которому можно создавать базы, например
postgresql://postgres:pass@127.0.0.1:5432/postgres) — работа с настоящей базой: каждая проверка получает
свою временную базу и удаляет её после себя.
"""

import copy
import json
import os
import uuid

import pytest
from fastapi.testclient import TestClient

from app import codegen, dbsettings, packages, storage
from app.graph import GraphError, compile_project
from app.main import app as platform_app
from app.runtime import Database, normalize_db_url
from tests.test_engine import EXAMPLE, exported_app

PG_ADMIN = os.environ.get("DRAGAPI_PG_TEST_URL")
URL = "postgresql://app:secret@db.example.com:5432/shop"


def pg_graph(url=URL, schema="manage", secret=True, **db):
    g = copy.deepcopy(EXAMPLE)
    g["env"] = g["env"] + [{"key": "DATABASE_URL", "value": url, "secret": secret}]
    g["database"] = {"engine": "postgres", "url_env": "DATABASE_URL", "schema": schema, **db}
    return g


# ---------- без сервера ----------


def test_settings_validation():
    g = pg_graph()
    g["env"] = [e for e in g["env"] if e["key"] != "DATABASE_URL"]
    with pytest.raises(GraphError, match="заведите переменную окружения DATABASE_URL"):
        compile_project(g)
    with pytest.raises(GraphError, match="должна начинаться с postgresql://"):
        compile_project(pg_graph(url="mysql://x"))
    with pytest.raises(GraphError, match="неизвестный тип"):
        compile_project({**EXAMPLE, "database": {"engine": "oracle"}})
    p = compile_project(pg_graph(secret=False))
    assert any("отметьте переменную секретом" in w.message for w in p.warnings)
    p = compile_project(pg_graph())
    assert p.database.postgres and p.database.schema == "manage" and not p.database.tests_use_db
    assert compile_project(EXAMPLE).database.engine == "sqlite"


def test_url_helpers():
    assert normalize_db_url("postgres://a:b@h/d") == "postgresql+psycopg://a:b@h/d"
    assert normalize_db_url("postgresql://a:b@h/d") == "postgresql+psycopg://a:b@h/d"
    assert normalize_db_url("sqlite:///x.db") == "sqlite:///x.db"
    assert dbsettings.redact("postgresql://app:secret@h:5432/d") == "postgresql://app:***@h:5432/d"
    url, changed = dbsettings.for_container("postgresql://app:p%40ss@localhost:5432/d")
    assert changed and url == "postgresql://app:p%40ss@host.docker.internal:5432/d"
    assert dbsettings.for_container(URL) == (URL, False)


def test_connection_errors_are_readable():
    class E(Exception):
        pass
    assert dbsettings.connection_error(E('FATAL:  password authentication failed for user "app"')) == \
        "неверный пользователь или пароль"
    assert dbsettings.connection_error(E("connection refused")).startswith("сервер не отвечает")


def test_export_uses_postgres_and_hides_url():
    files = codegen.generate_files(compile_project(pg_graph()))
    assert "psycopg[binary]" in files["requirements.txt"]
    assert "db = Database(os.environ.get('DATABASE_URL', 'sqlite:///./app.db'), MODELS)" in files["main.py"]
    assert "secret@" not in json.dumps(files)
    assert "DATABASE_URL=postgresql://" in files[".env.example"]
    g = pg_graph(schema="existing")
    g["env"][-1]["key"] = g["database"]["url_env"] = "CRM_DB"
    main = codegen.generate_files(compile_project(g))["main.py"]
    assert "os.environ.get('CRM_DB') or os.environ.get('DATABASE_URL'" in main
    assert "apply_migrations=False" in main and "check_schema()" in main
    assert "psycopg" not in codegen.generate_files(compile_project(EXAMPLE))["requirements.txt"]


def test_project_file_never_carries_url():
    g = pg_graph(secret=False)        # даже не отмеченная секретом строка подключения не экспортируется
    pkg = packages.export_package({**g, "kind": "api"})
    assert "secret@" not in json.dumps(pkg)
    assert "DATABASE_URL" in pkg["secrets"] and pkg["project"]["database"]["engine"] == "postgres"


@pytest.fixture
def platform(tmp_path, monkeypatch):
    monkeypatch.setattr(storage, "DATA_DIR", tmp_path / "projects")
    monkeypatch.setattr(storage, "DB_DIR", tmp_path / "db")
    with TestClient(platform_app, headers={"X-DragAPI": "1"}) as c:
        yield c


def _project(c, graph):
    pid = c.post("/api/projects", json={"name": "PG"}).json()["id"]
    body = {k: graph[k] for k in ("name", "nodes", "edges", "models", "env", "database") if k in graph}
    assert c.put(f"/api/projects/{pid}", json=body).status_code == 200
    return pid


def test_editor_test_requests_use_sqlite_copy(platform):
    """Сервер PostgreSQL недоступен, а тестовый запрос из редактора всё равно работает — на SQLite-копии."""
    c = platform
    g = pg_graph(url="postgresql://app:secret@127.0.0.1:1/none")
    pid = _project(c, g)
    graph = c.get(f"/api/projects/{pid}").json()
    r = c.post(f"/api/projects/{pid}/test-request", json={"graph": graph, "endpoint": "create",
                                                         "request": {"body_type": "json", "body": '{"item": "pen", "price": 3}'}})
    assert r.status_code == 200 and r.json()["status"] == 201, r.text
    run = c.post(f"/api/projects/{pid}/run")     # «Запустить» — уже в PostgreSQL, а его нет
    assert run.status_code == 400 and "сервер не отвечает" in run.json()["detail"]["message"]
    r = c.post(f"/api/projects/{pid}/database/inspect", json={})
    assert r.json()["ok"] is False and "secret" not in r.text


# ---------- с настоящим PostgreSQL ----------

pg = pytest.mark.skipif(not PG_ADMIN, reason="нет DRAGAPI_PG_TEST_URL — PostgreSQL для тестов не задан")


@pytest.fixture
def pg_url():
    import sqlalchemy as sa
    from sqlalchemy.engine import make_url
    name = f"dragapi_test_{uuid.uuid4().hex[:8]}"
    admin = sa.create_engine(normalize_db_url(PG_ADMIN), isolation_level="AUTOCOMMIT")
    with admin.connect() as conn:
        conn.execute(sa.text(f'CREATE DATABASE "{name}"'))
    url = make_url(PG_ADMIN).set(database=name).render_as_string(hide_password=False)
    try:
        yield url
    finally:
        with admin.connect() as conn:
            conn.execute(sa.text(f'DROP DATABASE IF EXISTS "{name}" WITH (FORCE)'))
        admin.dispose()


def _sql(url, *statements):
    import sqlalchemy as sa
    e = sa.create_engine(normalize_db_url(url))
    with e.begin() as conn:
        out = [conn.execute(sa.text(s)) for s in statements]
        rows = [r.all() if r.returns_rows else None for r in out]
    e.dispose()
    return rows


@pg
def test_managed_postgres_end_to_end(platform, pg_url):
    c = platform
    pid = _project(c, pg_graph(url=pg_url))
    run = c.post(f"/api/projects/{pid}/run").json()
    assert [m["op"] for m in run["migrations"]] == ["create_table", "create_table"]
    assert c.post(f"/run/{pid}/orders", json={"item": "Книга", "price": 10, "qty": 2}).status_code == 201
    assert c.post(f"/run/{pid}/orders", json={"item": "ручка", "price": 1}).status_code == 201
    assert _sql(pg_url, "SELECT item, total FROM orders ORDER BY id")[0] == [("Книга", 20.0), ("ручка", 1.0)]
    assert {"model": "orders", "count": 2} in c.get(f"/api/projects/{pid}/data").json()
    # «содержит» без учёта регистра — как в SQLite
    db = Database(pg_url, [m.as_dict() for m in compile_project(EXAMPLE).models], apply_migrations=False)
    assert [r["item"] for r in db.list("orders", [("item", "contains", "КНИ")])] == ["Книга"]
    # очистка таблицы — нумерация снова с 1
    assert c.delete(f"/api/projects/{pid}/data/orders").json() == {"ok": True}
    assert c.post(f"/run/{pid}/orders", json={"item": "pen", "price": 1}).json()["order"]["id"] == 1
    db.clear_all()
    assert db.insert("customers", {"name": "A"})["id"] == 1
    db.dispose()
    # миграции: переименование поля сохраняет данные
    g = pg_graph(url=pg_url)
    g["models"][1]["fields"][0]["name"] = "title"
    for n in g["nodes"]:
        n["data"] = json.loads(json.dumps(n["data"]).replace('"item"', '"title"').replace("item", "title"))
    c.post(f"/run/{pid}/orders", json={"item": "pen", "price": 1})
    db = Database(pg_url, [m.as_dict() for m in compile_project(g).models])
    assert [o["op"] for o in db.applied] == ["rename_column"]
    assert db.get("orders", 1)["title"] == "pen"
    db.dispose()


@pg
def test_existing_database_import_and_types(platform, pg_url):
    c = platform
    _sql(pg_url,
         "CREATE TABLE clients (id serial PRIMARY KEY, full_name text, vip boolean, joined date, ext uuid, meta jsonb)",
         "CREATE TABLE invoices (id bigserial PRIMARY KEY, client_id int REFERENCES clients(id) ON DELETE CASCADE, "
         "amount numeric(10,2), \"Bad Col\" text)",
         "CREATE TABLE audit (event text)",
         "INSERT INTO clients (full_name, vip, joined) VALUES ('Иван', true, '2026-01-15')")
    g = {"name": "CRM", "nodes": [], "edges": [], "models": [], "env": [{"key": "CRM_DB", "value": pg_url, "secret": True}],
         "database": {"engine": "postgres", "url_env": "CRM_DB", "schema": "existing"}}
    pid = _project(c, g)
    info = c.post(f"/api/projects/{pid}/database/inspect", json={}).json()
    assert info["ok"] and info["server"].startswith("PostgreSQL")
    assert [t["name"] for t in info["tables"]] == ["clients", "invoices"]
    assert info["skipped"] == [{"table": "audit", "reason": "нет целочисленного первичного ключа id"}]
    inv = next(t for t in info["tables"] if t["name"] == "invoices")
    assert inv["skipped_columns"] == ["Bad Col"]
    ref = next(f for f in inv["fields"] if f["name"] == "client_id")
    assert (ref["type"], ref["ref"], ref["on_delete"]) == ("ref", "clients", "cascade")
    models = info["models"]
    types = {f["name"]: f["type"] for f in models[0]["fields"]}
    assert types == {"full_name": "str", "vip": "bool", "joined": "str", "ext": "str", "meta": "str"}

    db = Database(pg_url, models, apply_migrations=False)
    assert db.check_schema() == []
    row = db.insert("clients", {"full_name": "Anna", "joined": "2026-03-01", "ext": str(uuid.UUID(int=7)),
                                "meta": '{"tier": 2}'})
    assert str(row["joined"]) == "2026-03-01" and row["meta"] == {"tier": 2}
    assert [r["full_name"] for r in db.list("clients", [("joined", "gte", "2026-02-01")])] == ["Anna"]
    assert db.list("clients", [("ext", "eq", str(uuid.UUID(int=7)))])[0]["id"] == row["id"]
    db.dispose()

    # схема существующей базы не меняется: ни служебной таблицы, ни новых колонок
    bad = copy.deepcopy(models)
    bad[0]["fields"].append({"id": "x", "name": "phone", "type": "str"})
    assert Database(pg_url, bad, apply_migrations=False).check_schema() == ["в таблице «clients» нет колонок: phone"]
    tables = _sql(pg_url, "SELECT tablename FROM pg_tables WHERE schemaname = 'public' ORDER BY 1")[0]
    assert [t[0] for t in tables] == ["audit", "clients", "invoices"]
    r = c.post(f"/api/projects/{pid}/schema/drop-extra", json={"models": models})
    assert r.status_code == 409


@pg
def test_exported_app_on_postgres(tmp_path, monkeypatch, pg_url):
    app = exported_app(pg_graph(url=pg_url), tmp_path, monkeypatch)
    monkeypatch.setenv("DATABASE_URL", pg_url)   # exported_app ставит SQLite — возвращаем PostgreSQL
    import importlib.util
    spec = importlib.util.spec_from_file_location("exported_pg", tmp_path / "export" / "main.py")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    c = TestClient(mod.app)
    assert c.post("/orders", json={"item": "pen", "price": 3}).status_code == 201
    assert _sql(pg_url, "SELECT item FROM orders")[0] == [("pen",)]
    assert app is not None
