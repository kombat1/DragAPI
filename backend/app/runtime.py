# --- DragAPI runtime: база данных и авторизация ---
# Этот модуль самодостаточен: генератор кода вставляет его в экспортированный main.py
# дословно, поэтому превью и экспорт используют одну и ту же реализацию.

import hmac
import ipaddress
import json

import sqlalchemy as sa

DB_TYPES = {"str": sa.String, "int": sa.Integer, "float": sa.Float, "bool": sa.Boolean}


class Database:
    """Таблицы из моделей проекта. У каждой модели есть автоматический первичный ключ id."""

    def __init__(self, url, models):
        connect_args = {"check_same_thread": False} if url.startswith("sqlite") else {}
        self.engine = sa.create_engine(url, connect_args=connect_args)
        self.metadata = sa.MetaData()
        self.tables = {}
        for model in models:
            columns = [sa.Column("id", sa.Integer, primary_key=True, autoincrement=True)]
            columns += [sa.Column(f["name"], DB_TYPES[f["type"]], nullable=True) for f in model["fields"]]
            # sqlite_autoincrement: id удалённых записей не выдаются повторно (как serial в PostgreSQL)
            self.tables[model["name"]] = sa.Table(model["name"], self.metadata, *columns, sqlite_autoincrement=True)
        self._sync_schema()

    def _sync_schema(self):
        """Создаёт недостающие таблицы и добавляет новые колонки (типы существующих не меняет)."""
        self.metadata.create_all(self.engine)
        inspector = sa.inspect(self.engine)
        with self.engine.begin() as conn:
            for name, table in self.tables.items():
                existing = {c["name"] for c in inspector.get_columns(name)}
                for col in table.columns:
                    if col.name not in existing:
                        col_type = col.type.compile(dialect=self.engine.dialect)
                        conn.execute(sa.text(f'ALTER TABLE "{name}" ADD COLUMN "{col.name}" {col_type}'))

    def _row(self, row):
        return dict(row._mapping) if row is not None else None

    def _values(self, model, values):
        table = self.tables[model]
        return {k: v for k, v in values.items() if k in table.c and k != "id"}

    def insert(self, model, values):
        table = self.tables[model]
        with self.engine.begin() as conn:
            result = conn.execute(table.insert().values(**self._values(model, values)))
            new_id = result.inserted_primary_key[0]
            return self._row(conn.execute(table.select().where(table.c.id == new_id)).first())

    def get(self, model, record_id):
        table = self.tables[model]
        with self.engine.connect() as conn:
            return self._row(conn.execute(table.select().where(table.c.id == record_id)).first())

    def list(self, model, filters=None, limit=100, order_by="id", descending=False):
        table = self.tables[model]
        query = table.select()
        for field, value in (filters or {}).items():
            query = query.where(table.c[field] == value)
        column = table.c[order_by]
        query = query.order_by(column.desc() if descending else column.asc())
        query = query.limit(max(0, min(int(limit), 1000)))
        with self.engine.connect() as conn:
            return [self._row(r) for r in conn.execute(query)]

    def update(self, model, record_id, values):
        table = self.tables[model]
        values = self._values(model, values)
        with self.engine.begin() as conn:
            if values:
                conn.execute(table.update().where(table.c.id == record_id).values(**values))
            return self._row(conn.execute(table.select().where(table.c.id == record_id)).first())

    def delete(self, model, record_id):
        table = self.tables[model]
        with self.engine.begin() as conn:
            return conn.execute(table.delete().where(table.c.id == record_id)).rowcount > 0

    def count(self, model):
        table = self.tables[model]
        with self.engine.connect() as conn:
            return conn.execute(sa.select(sa.func.count()).select_from(table)).scalar_one()

    def clear(self, model):
        """Удалить все записи и сбросить счётчик id: после очистки нумерация снова с 1."""
        with self.engine.begin() as conn:
            conn.execute(self.tables[model].delete())
            if self.engine.dialect.name == "sqlite":
                if sa.inspect(conn).has_table("sqlite_sequence"):
                    conn.execute(sa.text("DELETE FROM sqlite_sequence WHERE name = :n"), {"n": model})

    def dispose(self):
        self.engine.dispose()


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
