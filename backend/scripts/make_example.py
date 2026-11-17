"""Пересобирает пример-проект data/projects/example-shop.json.

Запуск: .venv/bin/python scripts/make_example.py
"""

import json
from pathlib import Path

OUT = Path(__file__).resolve().parent.parent / "data" / "projects" / "example-shop.json"
ROW = 250
COL = 290

nodes, edges = [], []


def node(nid, ntype, col, y, data):
    nodes.append({"id": nid, "type": ntype, "position": {"x": 40 + col * COL, "y": y}, "data": data})
    return nid


def link(a, b, handle=None):
    e = {"id": f"e-{a}-{b}", "source": a, "target": b}
    if handle:
        e["sourceHandle"] = handle
    edges.append(e)


def chain(row, items, start_col=0):
    """items: [(id, type, data)] — линейная цепочка в строке row; возвращает id последнего блока."""
    y = 40 + row * ROW
    ids = [node(nid, t, start_col + i, y, d) for i, (nid, t, d) in enumerate(items)]
    for a, b in zip(ids, ids[1:]):
        link(a, b)
    return ids[-1], start_col + len(ids), y


def branch(cond_id, col, y, yes, no):
    """Два ответа после условия: «да» выше, «нет» ниже."""
    link(cond_id, node(yes[0], "response", col, y - 70, yes[1]), "true")
    link(cond_id, node(no[0], "response", col, y + 90, no[1]), "false")


def body(**kw):
    return [{"key": k, "expr": v} for k, v in kw.items()]


path_id = ("input", {"source": "path", "fields": [{"name": "order_id", "type": "int", "required": True}]})

chain(0, [
    ("hello", "endpoint", {"method": "GET", "path": "/hello", "summary": "Приветствие"}),
    ("hello-in", "input", {"source": "query", "fields": [
        {"name": "name", "type": "str", "required": False, "default": "world"}]}),
    ("hello-logic", "logic", {"assignments": [
        {"target": "greeting", "expr": "f'Hello, {name}! Это {env[\"SHOP_NAME\"]}'"}]}),
    ("hello-out", "response", {"status": 200, "body": body(message="greeting")}),
])

chain(1, [
    ("create", "endpoint", {"method": "POST", "path": "/orders", "summary": "Создать заказ"}),
    ("create-in", "input", {"source": "json", "fields": [
        {"name": "item", "type": "str", "required": True},
        {"name": "price", "type": "float", "required": True},
        {"name": "qty", "type": "int", "required": False, "default": 1}]}),
    ("create-check", "validate", {"rules": [
        {"field": "qty", "op": "min", "value": "1", "message": "Количество должно быть не меньше 1"},
        {"field": "price", "op": "min", "value": "0"},
        {"field": "item", "op": "min_len", "value": "2"}]}),
    ("create-logic", "logic", {"assignments": [{"target": "total", "expr": "round(price * qty, 2)"}]}),
    ("create-db", "db", {"model": "orders", "op": "insert", "target": "order", "values": [
        {"field": "item", "expr": "item"}, {"field": "price", "expr": "price"},
        {"field": "qty", "expr": "qty"}, {"field": "total", "expr": "total"},
        {"field": "status", "expr": "'new'"}]}),
    ("create-out", "response", {"status": 201, "body": body(order="order")}),
])

chain(2, [
    ("list", "endpoint", {"method": "GET", "path": "/orders", "summary": "Список заказов"}),
    ("list-in", "input", {"source": "query", "fields": [
        {"name": "limit", "type": "int", "required": False, "default": 50}]}),
    ("list-db", "db", {"model": "orders", "op": "list", "target": "orders", "filters": [],
                       "limit": "100", "order_by": "id", "descending": True}),
    ("list-out", "response", {"status": 200, "body": body(count="len(orders)", items="orders[:limit]")}),
])

last, col, y = chain(3, [
    ("get", "endpoint", {"method": "GET", "path": "/orders/{order_id}", "summary": "Заказ по id"}),
    ("get-in", *path_id),
    ("get-db", "db", {"model": "orders", "op": "get", "target": "order", "id_expr": "order_id"}),
    ("get-if", "condition", {"expr": "order is None"}),
])
branch(last, col, y,
       ("get-404", {"status": 404, "body": body(error="f'Заказ {order_id} не найден'")}),
       ("get-200", {"status": 200, "body": body(order="order")}))

last, col, y = chain(4, [
    ("delete", "endpoint", {"method": "DELETE", "path": "/orders/{order_id}", "summary": "Удалить заказ (нужен X-API-Key)"}),
    ("delete-auth", "auth", {"scheme": "api_key_header", "name": "X-API-Key", "secret_env": "API_KEY"}),
    ("delete-in", *path_id),
    ("delete-db", "db", {"model": "orders", "op": "delete", "target": "deleted", "id_expr": "order_id"}),
    ("delete-if", "condition", {"expr": "deleted"}),
])
branch(last, col, y,
       ("delete-200", {"status": 200, "body": body(deleted="order_id")}),
       ("delete-404", {"status": 404, "body": body(error="f'Заказ {order_id} не найден'")}))

STATS_CODE = """from collections import Counter


def run(data, env):
    words = [w.strip(".,!?:;()\\"'«»").lower() for w in data["text"].split()]
    words = [w for w in words if len(w) >= data["min_len"]]
    print(f"слов после фильтра: {len(words)}")
    top = Counter(words).most_common(data["top"])
    return {
        "word_count": len(words),
        "top_words": [{"word": w, "count": c} for w, c in top],
    }
"""

chain(5, [
    ("stats", "endpoint", {"method": "POST", "path": "/text/stats", "summary": "Статистика текста (Python-код)"}),
    ("stats-in", "input", {"source": "json", "fields": [
        {"name": "text", "type": "str", "required": True},
        {"name": "top", "type": "int", "required": False, "default": 3},
        {"name": "min_len", "type": "int", "required": False, "default": 3}]}),
    ("stats-check", "validate", {"rules": [
        {"field": "text", "op": "max_len", "value": "10000"},
        {"field": "top", "op": "min", "value": "1"},
        {"field": "top", "op": "max", "value": "20"}]}),
    ("stats-code", "code", {"code": STATS_CODE, "outputs": ["word_count", "top_words"],
                            "timeout": 2, "memory_mb": 128}),
    ("stats-out", "response", {"status": 200, "body": body(words="word_count", top="top_words")}),
])

chain(6, [
    ("upload", "endpoint", {"method": "POST", "path": "/upload", "summary": "Загрузка файла"}),
    ("upload-in", "input", {"source": "form", "fields": [
        {"name": "file", "type": "file", "required": True},
        {"name": "comment", "type": "str", "required": False, "default": ""}]}),
    ("upload-out", "response", {"status": 200, "body": body(
        filename="file['filename']", size="file['size']", comment="comment")}),
])

project = {
    "id": "example-shop",
    "name": "Пример: магазин",
    "models": [{"name": "orders", "fields": [
        {"name": "item", "type": "str"}, {"name": "price", "type": "float"},
        {"name": "qty", "type": "int"}, {"name": "total", "type": "float"},
        {"name": "status", "type": "str"}]}],
    "env": [
        {"key": "SHOP_NAME", "value": "DragAPI Shop", "secret": False},
        {"key": "API_KEY", "value": "dev-secret", "secret": True},
    ],
    "nodes": nodes,
    "edges": edges,
    "updated": 0,
}
OUT.write_text(json.dumps(project, ensure_ascii=False, indent=2), "utf-8")
print(f"{OUT}: {len(nodes)} блоков, {len(edges)} связей")
