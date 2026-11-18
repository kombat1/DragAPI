"""Каталог блоков — единый источник правды для бэкенда и фронтенда.

Фронтенд получает его через GET /api/blocks и по нему же проверяет,
какие порты можно соединять (типизация связей между блоками).
"""

# Типы портов. Соединять можно только выход и вход с пересекающимися типами.
#   request — «сырой» HTTP-запрос сразу после эндпоинта (данных ещё нет)
#   data    — контекст с уже извлечёнными полями
PORT_TYPES = {
    "request": "HTTP-запрос (ещё без извлечённых данных)",
    "data": "Контекст с полями запроса",
}

HTTP_METHODS = ["GET", "POST", "PUT", "PATCH", "DELETE"]
INPUT_SOURCES = ["json", "query", "path", "form"]
FIELD_TYPES = ["str", "int", "float", "bool", "file"]
VALIDATION_OPS = ["min", "max", "min_len", "max_len", "regex", "in"]
DB_OPS = ["insert", "get", "list", "update", "delete"]
MODEL_FIELD_TYPES = ["str", "int", "float", "bool", "ref"]
ON_DELETE = ["restrict", "cascade", "set_null"]
FILTER_OPS = ["eq", "ne", "gt", "gte", "lt", "lte", "contains", "startswith", "in", "is_null"]
AUTH_SCHEMES = ["api_key_header", "api_key_query", "bearer"]

BLOCKS = {
    "endpoint": {
        "title": "HTTP-эндпоинт",
        "description": "Метод + путь. Начало цепочки.",
        "color": "#6366f1",
        "inputs": [],
        "output": ["request"],
        "defaults": {"method": "GET", "path": "/hello", "summary": ""},
    },
    "input": {
        "title": "Входные данные",
        "description": "Поля из JSON-тела, query, path или form-data (включая файлы).",
        "color": "#0ea5e9",
        "inputs": ["request", "data"],
        "output": ["data"],
        "defaults": {
            "source": "query",
            "fields": [{"name": "name", "type": "str", "required": False, "default": "world"}],
        },
    },
    "validate": {
        "title": "Валидация",
        "description": "Правила для полей. При нарушении — ответ 422.",
        "color": "#f59e0b",
        "inputs": ["data"],
        "output": ["data"],
        "defaults": {"rules": []},
    },
    "logic": {
        "title": "Логика",
        "description": "Вычисляет новые поля по выражениям: total = price * qty.",
        "color": "#10b981",
        "inputs": ["data"],
        "output": ["data"],
        "defaults": {"assignments": [{"target": "greeting", "expr": "f'Hello, {name}!'"}]},
    },
    "auth": {
        "title": "Авторизация",
        "description": "Проверяет API-ключ или Bearer-токен из переменной окружения. Иначе — 401.",
        "color": "#64748b",
        "inputs": ["request", "data"],
        "output": ["data"],
        "defaults": {"scheme": "api_key_header", "name": "X-API-Key", "secret_env": "API_KEY"},
    },
    "db": {
        "title": "База данных",
        "description": "Создать, получить, найти, изменить или удалить запись модели.",
        "color": "#8b5cf6",
        "inputs": ["data"],
        "output": ["data"],
        "defaults": {
            "model": "", "op": "insert", "target": "record", "id_expr": "",
            "values": [], "filters": [], "limit": "100", "order_by": "id", "descending": False,
        },
    },
    "code": {
        "title": "Python-код",
        "description": "Своя логика на Python: run(data, env) возвращает dict с новыми полями.",
        "color": "#0f766e",
        "inputs": ["data"],
        "output": ["data"],
        "defaults": {
            "code": (
                "def run(data, env):\n"
                "    # data — поля запроса (dict), env — переменные окружения проекта.\n"
                "    # Верните dict только с полями из списка «Выходные поля».\n"
                "    words = str(data.get(\"text\", \"\")).split()\n"
                "    return {\"word_count\": len(words)}\n"
            ),
            "outputs": ["word_count"],
            "timeout": 2,
            "memory_mb": 256,
        },
    },
    "condition": {
        "title": "Условие",
        "description": "Ветвление: выход «да», если выражение истинно, иначе «нет».",
        "color": "#ec4899",
        "inputs": ["data"],
        "output": ["data"],
        "branches": [{"id": "true", "label": "да"}, {"id": "false", "label": "нет"}],
        "defaults": {"expr": "record is None"},
    },
    "scenario": {
        "title": "Запустить сценарий",
        "description": "Запускает сценарий автоматизации (на компьютере с агентом) и ждёт результат.",
        "color": "#7c3aed",
        "inputs": ["data"],
        "output": ["data"],
        "defaults": {"project": "", "scenario": "main", "params": [], "target": "scenario",
                     "token_env": "SCENARIO_TOKEN", "url_env": "DRAGAPI_URL"},
    },
    "response": {
        "title": "Ответ",
        "description": "Статус и JSON-тело. Пустое тело — вернуть весь контекст.",
        "color": "#ef4444",
        "inputs": ["request", "data"],
        "output": [],
        "defaults": {"status": 200, "body": [{"key": "message", "expr": "greeting"}]},
    },
}


def catalog() -> dict:
    return {
        "blocks": BLOCKS,
        "port_types": PORT_TYPES,
        "http_methods": HTTP_METHODS,
        "input_sources": INPUT_SOURCES,
        "field_types": FIELD_TYPES,
        "validation_ops": VALIDATION_OPS,
        "db_ops": DB_OPS,
        "model_field_types": MODEL_FIELD_TYPES,
        "on_delete": ON_DELETE,
        "filter_ops": FILTER_OPS,
        "auth_schemes": AUTH_SCHEMES,
    }


def can_connect(source_type: str, target_type: str) -> bool:
    src = BLOCKS.get(source_type)
    dst = BLOCKS.get(target_type)
    if not src or not dst:
        return False
    return bool(set(src["output"]) & set(dst["inputs"]))
