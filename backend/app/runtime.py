# --- DragAPI runtime: база данных и авторизация ---
# Этот модуль самодостаточен: генератор кода вставляет его в экспортированный main.py
# дословно, поэтому превью и экспорт используют одну и ту же реализацию.

import hmac
import ipaddress
import json

import sqlalchemy as sa

DB_TYPES = {"str": sa.String, "int": sa.Integer, "float": sa.Float, "bool": sa.Boolean, "ref": sa.Integer}
ON_DELETE = {"restrict": "RESTRICT", "cascade": "CASCADE", "set_null": "SET NULL"}
FILTER_OPS = ("eq", "ne", "gt", "gte", "lt", "lte", "contains", "startswith", "in", "is_null")
SCHEMA_TABLE = "_dragapi_schema"


class DatabaseError(Exception):
    """Ошибка данных, понятная клиенту API (нарушена связь, нет записи и т.п.) — отдаётся как 409."""


def _field_id(field):
    return field.get("id") or "name:" + field["name"]


def _model_id(model):
    return model.get("id") or "name:" + model["name"]


def _ref_name(field_name):
    """customer_id -> customer: под этим именем подтягивается связанная запись."""
    return field_name[:-3] if field_name.endswith("_id") and len(field_name) > 3 else field_name + "_obj"


class Database:
    """Таблицы из моделей проекта. У каждой модели есть автоматический первичный ключ id.

    Схема сверяется со снимком в таблице _dragapi_schema: благодаря постоянным id моделей и полей
    переименование — это переименование (данные сохраняются), смена типа перестраивает таблицу.
    """

    def __init__(self, url, models, apply_migrations=True):
        connect_args = {"check_same_thread": False} if url.startswith("sqlite") else {}
        self.engine = sa.create_engine(url, connect_args=connect_args)
        if self.engine.dialect.name == "sqlite":
            # внешние ключи в SQLite включаются на каждое соединение; на выдаче из пула — тоже,
            # чтобы соединение, где их временно выключали (миграция, очистка), не вернулось без проверки
            @sa.event.listens_for(self.engine, "connect")
            def _fk_on_connect(dbapi_conn, _record):
                dbapi_conn.execute("PRAGMA foreign_keys=ON")

            @sa.event.listens_for(self.engine, "checkout")
            def _fk_on_checkout(dbapi_conn, _record, _proxy):
                dbapi_conn.execute("PRAGMA foreign_keys=ON")
        self.models = {m["name"]: m for m in models}
        self.metadata = sa.MetaData()
        self.tables = {}
        for model in models:
            self.tables[model["name"]] = self._table(model, model["name"], self.metadata)
        self.applied = self.migrate() if apply_migrations else []

    def _table(self, model, name, metadata):
        columns = [sa.Column("id", sa.Integer, primary_key=True, autoincrement=True)]
        for f in model["fields"]:
            if f["type"] == "ref":
                fk = sa.ForeignKey(f'{f["ref"]}.id', ondelete=ON_DELETE[f.get("on_delete") or "restrict"])
                columns.append(sa.Column(f["name"], sa.Integer, fk, nullable=True))
            else:
                columns.append(sa.Column(f["name"], DB_TYPES[f["type"]], nullable=True))
        # sqlite_autoincrement: id удалённых записей не выдаются повторно (как serial в PostgreSQL)
        return sa.Table(name, metadata, *columns, sqlite_autoincrement=True)

    # ---------- миграции ----------

    def _snapshot(self, conn):
        insp = sa.inspect(conn)
        if not insp.has_table(SCHEMA_TABLE):
            return None
        rows = conn.execute(sa.text(f"SELECT model_id, field_id, model_name, field_name, type, ref "
                                    f"FROM {SCHEMA_TABLE}")).mappings().all()
        snap = {}
        for r in rows:
            m = snap.setdefault(r["model_id"], {"name": r["model_name"], "fields": {}})
            if r["field_id"]:
                m["fields"][r["field_id"]] = {"name": r["field_name"], "type": r["type"], "ref": r["ref"]}
        return snap

    def _bootstrap_snapshot(self, conn):
        """Снимка ещё нет (новая БД или БД до миграций): берём то, что реально лежит в таблицах."""
        insp = sa.inspect(conn)
        snap = {}
        for name, model in self.models.items():
            if not insp.has_table(name):
                continue
            existing = {c["name"] for c in insp.get_columns(name)}
            snap[_model_id(model)] = {"name": name, "fields": {
                _field_id(f): {"name": f["name"], "type": f["type"], "ref": f.get("ref")}
                for f in model["fields"] if f["name"] in existing}}
        return snap

    def plan(self, drop_extra=False):
        """Операции, которые приведут БД к моделям: [{op, model, ...}]. Ничего не меняет."""
        with self.engine.connect() as conn:
            snap = self._snapshot(conn)
            if snap is None:
                snap = self._bootstrap_snapshot(conn)
            return self._diff(conn, snap, drop_extra)

    def _diff(self, conn, snap, drop_extra):
        insp = sa.inspect(conn)
        ops = []
        for name, model in self.models.items():
            old = snap.get(_model_id(model))
            table_name = old["name"] if old else name
            if old and old["name"] != name:
                ops.append({"op": "rename_table", "model": name, "old": old["name"]})
            if not insp.has_table(table_name):
                ops.append({"op": "create_table", "model": name})
                continue
            columns = {c["name"] for c in insp.get_columns(table_name)}
            old_fields = old["fields"] if old else {}
            seen_columns = {"id"}
            for f in model["fields"]:
                prev = old_fields.get(_field_id(f))
                if prev is None and f["name"] in columns:
                    # поле без записи в снимке, но колонка с таким именем есть (поле получило id позже) —
                    # это то же поле; тип сверяем по снимку, если он есть под старым id «name:…»
                    prev = old_fields.get("name:" + f["name"]) or {"name": f["name"], "type": f["type"],
                                                                     "ref": f.get("ref")}
                    if prev["name"] != f["name"]:
                        prev = {"name": f["name"], "type": f["type"], "ref": f.get("ref")}
                if prev is None or prev["name"] not in columns:
                    ops.append({"op": "add_column", "model": name, "field": f["name"], "type": f["type"]})
                    seen_columns.add(f["name"])
                    continue
                seen_columns.add(prev["name"])
                if prev["name"] != f["name"]:
                    ops.append({"op": "rename_column", "model": name, "old": prev["name"], "field": f["name"]})
                if prev["type"] != f["type"] or (f["type"] == "ref" and prev.get("ref") != f.get("ref")):
                    ops.append({"op": "change_type", "model": name, "field": f["name"],
                                "old_type": prev["type"], "type": f["type"]})
            for extra in sorted(columns - seen_columns):
                ops.append({"op": "drop_column" if drop_extra else "extra_column", "model": name, "field": extra})
        return ops

    def migrate(self, drop_extra=False):
        """Применить план. Возвращает применённые операции."""
        with self.engine.connect() as conn:
            snap = self._snapshot(conn)
            if snap is None:
                snap = self._bootstrap_snapshot(conn)
            ops = self._diff(conn, snap, drop_extra)
        applied = []
        sqlite = self.engine.dialect.name == "sqlite"
        with self.engine.connect() as conn:
            if sqlite:  # вне транзакции: внутри неё SQLite эту настройку игнорирует
                conn.exec_driver_sql("PRAGMA foreign_keys=OFF")
            for o in ops:
                if o["op"] == "rename_table":
                    conn.execute(sa.text(f'ALTER TABLE "{o["old"]}" RENAME TO "{o["model"]}"'))
                    applied.append(o)
            for o in ops:
                if o["op"] == "rename_column":
                    conn.execute(sa.text(f'ALTER TABLE "{o["model"]}" RENAME COLUMN "{o["old"]}" TO "{o["field"]}"'))
                    applied.append(o)
            for name in self.models:
                table_ops = [o for o in ops if o["model"] == name]
                if any(o["op"] == "create_table" for o in table_ops):
                    self.tables[name].create(conn)
                    applied += table_ops
                elif any(o["op"] in ("change_type", "drop_column") for o in table_ops) or (
                        sqlite and any(o["op"] == "add_column" and o["type"] == "ref" for o in table_ops)):
                    self._rebuild(conn, name, table_ops)
                    applied += [o for o in table_ops if o["op"] not in ("rename_table", "rename_column", "extra_column")]
                else:
                    for o in table_ops:
                        if o["op"] == "add_column":
                            col = self.tables[name].c[o["field"]]
                            ddl = col.type.compile(dialect=self.engine.dialect)
                            if o["type"] == "ref":
                                ref = next(f for f in self.models[name]["fields"] if f["name"] == o["field"])["ref"]
                                ddl += f' REFERENCES "{ref}"(id)'
                            conn.execute(sa.text(f'ALTER TABLE "{name}" ADD COLUMN "{o["field"]}" {ddl}'))
                            applied.append(o)
            self._save_snapshot(conn)
            conn.commit()
            if sqlite:
                conn.exec_driver_sql("PRAGMA foreign_keys=ON")
                conn.commit()
        return applied

    def _rebuild(self, conn, name, ops):
        """Пересобрать таблицу по модели: смена типа, удаление колонки, внешний ключ (SQLite)."""
        if self.engine.dialect.name != "sqlite":
            for o in ops:
                col = self.tables[name].c.get(o.get("field"))
                if o["op"] == "change_type":
                    ddl = col.type.compile(dialect=self.engine.dialect)
                    conn.execute(sa.text(f'ALTER TABLE "{name}" ALTER COLUMN "{o["field"]}" TYPE {ddl} '
                                         f'USING "{o["field"]}"::{ddl}'))
                elif o["op"] == "drop_column":
                    conn.execute(sa.text(f'ALTER TABLE "{name}" DROP COLUMN "{o["field"]}"'))
                elif o["op"] == "add_column":
                    ddl = col.type.compile(dialect=self.engine.dialect)
                    conn.execute(sa.text(f'ALTER TABLE "{name}" ADD COLUMN "{o["field"]}" {ddl}'))
            return
        tmp_name = f"_new_{name}"
        existing_cols = {c["name"]: c for c in sa.inspect(conn).get_columns(name)}
        model_names = {f["name"] for f in self.models[name]["fields"]} | {"id"}
        dropped = {o["field"] for o in ops if o["op"] == "drop_column"}
        # колонки, которых нет в модели, сохраняем вместе с данными — удаляются только явно (drop_column)
        keep_extra = [c for n, c in existing_cols.items() if n not in model_names and n not in dropped]
        tmp_meta = sa.MetaData()
        for f in self.models[name]["fields"]:  # заглушки таблиц, на которые ссылаются внешние ключи
            if f["type"] == "ref" and f["ref"] not in tmp_meta.tables:
                sa.Table(f["ref"], tmp_meta, sa.Column("id", sa.Integer, primary_key=True))
        tmp = self._table(self.models[name], tmp_name, tmp_meta)
        for c in keep_extra:
            tmp.append_column(sa.Column(c["name"], c["type"], nullable=True))
        tmp.create(conn)
        sql_types = {"str": "TEXT", "int": "INTEGER", "float": "REAL", "bool": "INTEGER", "ref": "INTEGER"}
        targets, sources = ["id"], ["id"]
        for f in self.models[name]["fields"]:
            if f["name"] in existing_cols:
                targets.append(f'"{f["name"]}"')
                sources.append(f'CAST("{f["name"]}" AS {sql_types[f["type"]]})')
        for c in keep_extra:
            targets.append(f'"{c["name"]}"')
            sources.append(f'"{c["name"]}"')
        conn.execute(sa.text(f'INSERT INTO "{tmp_name}" ({", ".join(targets)}) '
                             f'SELECT {", ".join(sources)} FROM "{name}"'))
        seq = conn.execute(sa.text("SELECT seq FROM sqlite_sequence WHERE name = :n"), {"n": name}).scalar() \
            if sa.inspect(conn).has_table("sqlite_sequence") else None
        conn.execute(sa.text(f'DROP TABLE "{name}"'))
        conn.execute(sa.text(f'ALTER TABLE "{tmp_name}" RENAME TO "{name}"'))
        if seq is not None:  # счётчик id не откатывается назад
            conn.execute(sa.text("UPDATE sqlite_sequence SET seq = max(seq, :s) WHERE name = :n"), {"s": seq, "n": name})

    def _save_snapshot(self, conn):
        conn.execute(sa.text(f"CREATE TABLE IF NOT EXISTS {SCHEMA_TABLE} (model_id TEXT, field_id TEXT, "
                             f"model_name TEXT, field_name TEXT, type TEXT, ref TEXT, PRIMARY KEY (model_id, field_id))"))
        conn.execute(sa.text(f"DELETE FROM {SCHEMA_TABLE}"))
        rows = []
        for name, model in self.models.items():
            rows.append({"m": _model_id(model), "f": "", "mn": name, "fn": "", "t": "", "r": None})
            for f in model["fields"]:
                rows.append({"m": _model_id(model), "f": _field_id(f), "mn": name, "fn": f["name"],
                             "t": f["type"], "r": f.get("ref")})
        if rows:
            conn.execute(sa.text(f"INSERT INTO {SCHEMA_TABLE} VALUES (:m, :f, :mn, :fn, :t, :r)"), rows)

    # ---------- данные ----------

    def _row(self, row):
        return dict(row._mapping) if row is not None else None

    def _values(self, model, values):
        table = self.tables[model]
        return {k: v for k, v in values.items() if k in table.c and k != "id"}

    def _write(self, fn):
        try:
            with self.engine.begin() as conn:
                return fn(conn)
        except sa.exc.IntegrityError as e:
            text = str(e.orig).lower()
            if "foreign key" in text:
                raise DatabaseError("нарушена связь между записями: ссылка на несуществующую запись "
                                    "или удаление записи, на которую ссылаются другие") from None
            raise DatabaseError(f"нарушена целостность данных: {e.orig}") from None

    def _where(self, query, table, filters):
        if isinstance(filters, dict):  # совместимость: {поле: значение} — равенство
            filters = [(k, "eq", v) for k, v in filters.items()]
        for field, op, value in filters or []:
            if op != "is_null" and (value is None or value == ""):
                continue  # необязательный параметр не передан — фильтр не применяется
            col = table.c[field]
            if op == "eq":
                query = query.where(col.is_(None) if value is None else col == value)
            elif op == "ne":
                query = query.where(col.isnot(None) if value is None else col != value)
            elif op == "gt":
                query = query.where(col > value)
            elif op == "gte":
                query = query.where(col >= value)
            elif op == "lt":
                query = query.where(col < value)
            elif op == "lte":
                query = query.where(col <= value)
            elif op == "contains":
                query = query.where(col.contains(str(value), autoescape=True))
            elif op == "startswith":
                query = query.where(col.startswith(str(value), autoescape=True))
            elif op == "in":
                query = query.where(col.in_(list(value or [])))
            elif op == "is_null":
                query = query.where(col.is_(None) if value else col.isnot(None))
        return query

    def _include(self, conn, model, rows, include):
        """Подтянуть связанные записи: поле customer_id -> customer (одним запросом на связь)."""
        for field in include or []:
            spec = next(f for f in self.models[model]["fields"] if f["name"] == field)
            ref_table = self.tables[spec["ref"]]
            ids = {r[field] for r in rows if r and r.get(field) is not None}
            found = {}
            if ids:
                for rr in conn.execute(ref_table.select().where(ref_table.c.id.in_(ids))):
                    found[rr.id] = dict(rr._mapping)
            for r in rows:
                if r is not None:
                    r[_ref_name(field)] = found.get(r.get(field))
        return rows

    def insert(self, model, values):
        table = self.tables[model]

        def op(conn):
            result = conn.execute(table.insert().values(**self._values(model, values)))
            new_id = result.inserted_primary_key[0]
            return self._row(conn.execute(table.select().where(table.c.id == new_id)).first())
        return self._write(op)

    def get(self, model, record_id, include=None):
        table = self.tables[model]
        with self.engine.connect() as conn:
            row = self._row(conn.execute(table.select().where(table.c.id == record_id)).first())
            return self._include(conn, model, [row], include)[0]

    def list(self, model, filters=None, limit=100, order_by="id", descending=False, offset=0, include=None):
        table = self.tables[model]
        query = self._where(table.select(), table, filters)
        column = table.c[order_by]
        query = query.order_by(column.desc() if descending else column.asc(), table.c.id)
        query = query.limit(max(1, min(int(limit or 100), 1000))).offset(max(0, int(offset or 0)))
        with self.engine.connect() as conn:
            rows = [self._row(r) for r in conn.execute(query)]
            return self._include(conn, model, rows, include)

    def update(self, model, record_id, values):
        table = self.tables[model]
        values = self._values(model, values)

        def op(conn):
            if values:
                conn.execute(table.update().where(table.c.id == record_id).values(**values))
            return self._row(conn.execute(table.select().where(table.c.id == record_id)).first())
        return self._write(op)

    def delete(self, model, record_id):
        table = self.tables[model]
        return self._write(lambda conn: conn.execute(table.delete().where(table.c.id == record_id)).rowcount > 0)

    def count(self, model, filters=None):
        table = self.tables[model]
        query = self._where(sa.select(sa.func.count()).select_from(table), table, filters)
        with self.engine.connect() as conn:
            return conn.execute(query).scalar_one()

    def clear(self, model):
        """Удалить все записи и сбросить счётчик id: после очистки нумерация снова с 1."""
        def op(conn):
            conn.execute(self.tables[model].delete())
            if self.engine.dialect.name == "sqlite" and sa.inspect(conn).has_table("sqlite_sequence"):
                conn.execute(sa.text("DELETE FROM sqlite_sequence WHERE name = :n"), {"n": model})
        return self._write(op)

    def clear_all(self):
        """Очистить все таблицы проекта (связи не мешают: проверка ключей временно выключена)."""
        sqlite = self.engine.dialect.name == "sqlite"
        with self.engine.connect() as conn:
            if sqlite:  # вне транзакции, иначе SQLite игнорирует
                conn.exec_driver_sql("PRAGMA foreign_keys=OFF")
            for table in reversed(self.metadata.sorted_tables):
                conn.execute(table.delete())
            if sqlite and sa.inspect(conn).has_table("sqlite_sequence"):
                conn.execute(sa.text("DELETE FROM sqlite_sequence"))
            conn.commit()
            if sqlite:
                conn.exec_driver_sql("PRAGMA foreign_keys=ON")
                conn.commit()

    def dispose(self):
        self.engine.dispose()


def install_db_error_handler(app):
    """DatabaseError (нарушена связь и т.п.) -> 409 вместо 500."""
    from fastapi.responses import JSONResponse

    @app.exception_handler(DatabaseError)
    async def _handler(request, exc):
        return JSONResponse({"error": str(exc)}, status_code=409)


def check_auth(scheme, expected, header_value):
    """True, если запрос предъявил правильный секрет. Сравнение — за постоянное время."""
    if not expected or not header_value:
        return False
    if scheme == "bearer":
        prefix = "bearer "
        if not header_value.lower().startswith(prefix):
            return False
        header_value = header_value[len(prefix):].strip()
    return hmac.compare_digest(str(header_value).encode(), str(expected).encode())


def apply_code_result(result, outputs):
    """Проверяет результат run() блока «Python-код»: dict только с объявленными полями."""
    if not isinstance(result, dict):
        raise ValueError(f"run() должна вернуть dict, а вернула {type(result).__name__}")
    unknown = sorted(set(result) - set(outputs))
    if unknown:
        raise ValueError(f"run() вернула необъявленные поля: {', '.join(map(str, unknown))} "
                         f"(объявлены: {', '.join(outputs) or 'нет'})")
    return {name: result.get(name) for name in outputs}


class IngressFirewall:
    """ASGI-middleware: кто может обращаться к API (IP-адреса и подсети, первое совпавшее правило)."""

    def __init__(self, app, rules, default="allow", trust_proxy=False, on_event=None):
        self.app = app
        self.default = default
        self.trust_proxy = trust_proxy
        self.on_event = on_event
        self.rules = [(r["action"], ipaddress.ip_network(r["target"], strict=False), r["target"]) for r in rules]

    def client_ip(self, scope):
        if self.trust_proxy:
            for name, value in scope.get("headers", []):
                if name == b"x-forwarded-for":
                    return value.decode("latin-1").split(",")[0].strip()
        client = scope.get("client")
        return client[0] if client else ""

    def decide(self, ip_text):
        try:
            ip = ipaddress.ip_address(ip_text)
        except ValueError:
            return False, f"не удалось определить IP клиента ({ip_text!r})"
        if isinstance(ip, ipaddress.IPv6Address) and ip.ipv4_mapped:
            ip = ip.ipv4_mapped
        for i, (action, network, target) in enumerate(self.rules, 1):
            if ip.version == network.version and ip in network:
                return action == "allow", f"{'разрешено' if action == 'allow' else 'запрещено'} правилом {i} ({target})"
        return self.default == "allow", ("разрешено" if self.default == "allow" else "запрещено") + " по умолчанию"

    async def __call__(self, scope, receive, send):
        if scope["type"] not in ("http", "websocket"):
            return await self.app(scope, receive, send)
        ip = self.client_ip(scope)
        allowed, reason = self.decide(ip)
        if self.on_event is not None:
            self.on_event({"direction": "in", "ip": ip, "path": scope.get("path", ""), "allowed": allowed,
                           "reason": reason, "method": scope.get("method", "")})
        if allowed:
            return await self.app(scope, receive, send)
        if scope["type"] == "websocket":
            return await send({"type": "websocket.close", "code": 1008})
        body = json.dumps({"error": "forbidden", "detail": f"firewall: {ip} — {reason}"}, ensure_ascii=False).encode()
        await send({"type": "http.response.start", "status": 403,
                    "headers": [(b"content-type", b"application/json"), (b"content-length", str(len(body)).encode())]})
        await send({"type": "http.response.body", "body": body})
