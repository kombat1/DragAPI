"""Проверка графа проекта и его компиляция в план исполнения.

Граф хранится в формате React Flow: {nodes, edges} плюс модели данных и переменные
окружения проекта. Компиляция превращает его в CompiledProject: для каждого
эндпоинта — дерево Chain (шаги + ответ или ветвление). План потом либо
интерпретируется (interpreter.py), либо превращается в код (codegen.py).
"""

from __future__ import annotations

import ast
import keyword
import re
from dataclasses import dataclass, field
from typing import Any, Iterator, Union

from . import expr
from .pyenv import validate_packages
from .firewall import FirewallSettings
from .sandbox import SandboxSettings
from .blocks import (
    AUTH_SCHEMES, BLOCKS, DB_OPS, FIELD_TYPES, HTTP_METHODS, INPUT_SOURCES,
    MODEL_FIELD_TYPES, VALIDATION_OPS, can_connect,
)

IDENT_RE = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")
ENV_KEY_RE = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")
HEADER_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9\-]*$")
PATH_RE = re.compile(r"^/[A-Za-z0-9_\-/{}.]*$")
PATH_PARAM_RE = re.compile(r"{([A-Za-z_][A-Za-z0-9_]*)}")
RESERVED = {"ctx", "request", "re", "db", "env", "ENV", "MODELS", "id"}
MAX_CODE_LEN = 20_000


@dataclass
class Issue:
    node_id: str | None
    message: str
    level: str = "error"  # error | warning

    def as_dict(self):
        return {"node_id": self.node_id, "message": self.message, "level": self.level}


class GraphError(Exception):
    def __init__(self, issues: list[Issue]):
        self.issues = issues
        super().__init__("; ".join(i.message for i in issues))


# ---------- шаги плана ----------

@dataclass
class FieldSpec:
    name: str
    type: str
    required: bool = True
    default: Any = None


@dataclass
class InputStep:
    node_id: str
    source: str
    fields: list[FieldSpec]
    kind: str = "input"


@dataclass
class Rule:
    field: str
    op: str
    value: Any
    message: str


@dataclass
class ValidateStep:
    node_id: str
    rules: list[Rule]
    kind: str = "validate"


@dataclass
class Assignment:
    target: str
    source: str
    tree: ast.Expression


@dataclass
class LogicStep:
    node_id: str
    assignments: list[Assignment]
    kind: str = "logic"


@dataclass
class AuthStep:
    node_id: str
    scheme: str          # api_key_header | api_key_query | bearer
    name: str            # имя заголовка или query-параметра
    secret_env: str      # переменная окружения с ожидаемым секретом
    param: str = ""      # имя параметра обработчика (заполняется при компиляции)
    kind: str = "auth"


@dataclass
class DbStep:
    node_id: str
    model: str
    op: str
    target: str
    id_value: Assignment | None = None
    values: list[Assignment] = field(default_factory=list)
    filters: list[Assignment] = field(default_factory=list)
    limit: int = 100
    order_by: str = "id"
    descending: bool = False
    kind: str = "db"


@dataclass
class CodeStep:
    node_id: str
    code: str
    outputs: list[str]
    timeout: float
    memory_mb: int
    kind: str = "code"

    @property
    def module(self) -> str:
        """Имя модуля/функции в экспортированном проекте."""
        return "code_" + re.sub(r"\W", "_", self.node_id).lower()


@dataclass
class ResponseSpec:
    node_id: str
    status: int
    body: list[Assignment]  # пусто -> вернуть весь контекст


@dataclass
class Branch:
    node_id: str
    condition: Assignment
    then: "Chain"
    otherwise: "Chain"


Step = Union[InputStep, ValidateStep, LogicStep, AuthStep, DbStep, CodeStep]


@dataclass
class Chain:
    steps: list[Step]
    end: ResponseSpec | Branch


@dataclass
class EndpointPlan:
    node_id: str
    method: str
    path: str
    summary: str
    chain: Chain
    path_params: list[str] = field(default_factory=list)
    inputs: list[InputStep] = field(default_factory=list)
    auths: list[AuthStep] = field(default_factory=list)

    def fields_by_source(self, source: str) -> list[FieldSpec]:
        return [f for s in self.inputs if s.source == source for f in s.fields]

    def responses(self) -> list[ResponseSpec]:
        out: list[ResponseSpec] = []

        def walk(chain: Chain):
            if isinstance(chain.end, ResponseSpec):
                out.append(chain.end)
            else:
                walk(chain.end.then)
                walk(chain.end.otherwise)

        walk(self.chain)
        return out

    @property
    def default_status(self) -> int:
        statuses = [r.status for r in self.responses()]
        return next((s for s in statuses if 200 <= s < 300), statuses[0])

    @property
    def operation_id(self) -> str:
        slug = re.sub(r"[^a-z0-9]+", "_", self.path.lower()).strip("_") or "root"
        return f"{self.method.lower()}_{slug}"


@dataclass
class ModelSpec:
    name: str
    fields: list[dict]  # [{"name", "type"}]

    @property
    def field_names(self) -> set[str]:
        return {f["name"] for f in self.fields}

    def as_dict(self) -> dict:
        return {"name": self.name, "fields": self.fields}


@dataclass
class EnvVar:
    key: str
    value: str
    secret: bool


@dataclass
class CompiledProject:
    name: str
    plans: list[EndpointPlan]
    models: list[ModelSpec]
    env: list[EnvVar]
    warnings: list[Issue]
    sandbox: SandboxSettings = field(default_factory=SandboxSettings)
    packages: list[str] = field(default_factory=list)
    only_binary: bool = True
    firewall: FirewallSettings = field(default_factory=FirewallSettings)

    @property
    def env_values(self) -> dict[str, str]:
        return {e.key: e.value for e in self.env}

    @property
    def uses_db(self) -> bool:
        return bool(self.models)


# ---------- вспомогательное ----------

def _coerce_default(value: Any, ftype: str) -> Any:
    if value is None or value == "":
        return None
    try:
        if ftype == "int":
            return int(value)
        if ftype == "float":
            return float(value)
        if ftype == "bool":
            if isinstance(value, str):
                return value.strip().lower() in ("1", "true", "yes", "on")
            return bool(value)
        if ftype == "str":
            return str(value)
    except (TypeError, ValueError):
        raise ValueError(f"значение по умолчанию «{value}» не приводится к {ftype}") from None
    return None


def _check_ident(name: Any, what: str) -> str | None:
    if not isinstance(name, str) or not IDENT_RE.match(name):
        return f"{what} «{name}»: допустимы латиница, цифры и _, не с цифры"
    if keyword.iskeyword(name) or name in RESERVED or name in expr.FUNCTIONS:
        return f"{what} «{name}» — зарезервированное имя"
    return None


def _default_message(field_name, op, value) -> str:
    return {
        "min": f"{field_name} должно быть ≥ {value}",
        "max": f"{field_name} должно быть ≤ {value}",
        "min_len": f"длина {field_name} должна быть ≥ {value}",
        "max_len": f"длина {field_name} должна быть ≤ {value}",
        "regex": f"{field_name} не соответствует шаблону {value}",
        "in": f"{field_name} должно быть одним из: {value}",
    }[op]


def check_rule(rule: Rule, value: Any) -> bool:
    """Общая логика правил — используется интерпретатором (в экспорте она развёрнута в if-ы)."""
    if value is None:
        return True  # необязательное поле не задано — правило не применяем
    if rule.op == "min":
        return value >= rule.value
    if rule.op == "max":
        return value <= rule.value
    if rule.op == "min_len":
        return len(value) >= rule.value
    if rule.op == "max_len":
        return len(value) <= rule.value
    if rule.op == "regex":
        return re.fullmatch(rule.value, str(value)) is not None
    if rule.op == "in":
        return str(value) in rule.value
    return False


def iter_steps(chain: Chain) -> Iterator[Step]:
    yield from chain.steps
    if isinstance(chain.end, Branch):
        yield from iter_steps(chain.end.then)
        yield from iter_steps(chain.end.otherwise)


# ---------- компиляция ----------

def _compile_models(raw: Any, err) -> list[ModelSpec]:
    models: list[ModelSpec] = []
    seen: set[str] = set()
    for m in raw or []:
        name = m.get("name")
        if msg := _check_ident(name, "модель"):
            err(None, msg)
            continue
        if name.lower() in seen or name.lower().startswith("sqlite_"):
            err(None, f"модель «{name}» объявлена дважды или имя недопустимо")
            continue
        seen.add(name.lower())
        fields, fseen, ok = [], set(), True
        for f in m.get("fields") or []:
            fname, ftype = f.get("name"), f.get("type", "str")
            if msg := _check_ident(fname, f"модель {name}: поле"):
                err(None, msg + " (поле id создаётся автоматически)" if fname == "id" else msg)
                ok = False
                continue
            if fname in fseen:
                err(None, f"модель {name}: поле «{fname}» объявлено дважды")
                ok = False
                continue
            if ftype not in MODEL_FIELD_TYPES:
                err(None, f"модель {name}: у поля «{fname}» неизвестный тип {ftype}")
                ok = False
                continue
            fseen.add(fname)
            fields.append({"name": fname, "type": ftype})
        if ok:
            models.append(ModelSpec(name, fields))
    return models


def _compile_env(raw: Any, err) -> list[EnvVar]:
    out: list[EnvVar] = []
    seen: set[str] = set()
    for e in raw or []:
        key = e.get("key")
        if not isinstance(key, str) or not ENV_KEY_RE.match(key):
            err(None, f"переменная окружения «{key}»: допустимы латиница, цифры и _")
            continue
        if key in seen:
            err(None, f"переменная окружения «{key}» объявлена дважды")
            continue
        seen.add(key)
        out.append(EnvVar(key, "" if e.get("value") is None else str(e.get("value")), bool(e.get("secret"))))
    return out


def compile_project(graph: dict) -> CompiledProject:
    """Проверяет граф целиком. Ошибки — исключение GraphError, предупреждения — в результате."""
    issues: list[Issue] = []

    def err(node_id, msg):
        issues.append(Issue(node_id, msg))

    models = _compile_models(graph.get("models"), err)
    env = _compile_env(graph.get("env"), err)
    sandbox_settings, sandbox_errors = SandboxSettings.parse(graph.get("sandbox"))
    libraries = graph.get("libraries") or {}
    packages, package_errors = validate_packages(libraries.get("packages"))
    firewall, firewall_errors = FirewallSettings.parse(graph.get("firewall"), legacy_network=sandbox_settings.network)
    for message in sandbox_errors + package_errors + firewall_errors:
        err(None, message)
    nodes = {n["id"]: n for n in graph.get("nodes", []) if isinstance(n, dict) and "id" in n}

    for nid, n in nodes.items():
        if n.get("type") not in BLOCKS:
            err(nid, f"неизвестный тип блока «{n.get('type')}»")

    # (источник, выход) -> цель. У обычных блоков один выход (None), у условия — "true"/"false".
    outgoing: dict[tuple[str, str | None], str] = {}
    incoming: dict[str, str] = {}
    for e in graph.get("edges", []):
        s, t = e.get("source"), e.get("target")
        if s not in nodes or t not in nodes:
            err(None, f"связь {e.get('id')} ссылается на несуществующий блок")
            continue
        st, tt = nodes[s].get("type"), nodes[t].get("type")
        if not can_connect(st, tt):
            err(t, f"нельзя соединить «{BLOCKS.get(st, {}).get('title', st)}» → "
                   f"«{BLOCKS.get(tt, {}).get('title', tt)}»: несовместимые типы данных")
            continue
        branches = [b["id"] for b in BLOCKS.get(st, {}).get("branches", [])]
        handle = e.get("sourceHandle") if branches else None
        if branches and handle not in branches:
            err(s, f"связь из неизвестного выхода «{handle}»")
            continue
        if (s, handle) in outgoing:
            err(s, "из одного выхода может идти только одна связь")
            continue
        if t in incoming:
            err(t, "у блока может быть только один вход")
            continue
        outgoing[(s, handle)] = t
        incoming[t] = s

    model_map = {m.name: m for m in models}
    env_keys = {e.key for e in env}
    plans: list[EndpointPlan] = []
    reached: set[str] = set()
    routes_seen: dict[tuple[str, str], str] = {}
    endpoint_warnings: list[Issue] = []

    for nid, n in nodes.items():
        if n.get("type") != "endpoint":
            continue
        compiler = _EndpointCompiler(nid, nodes, outgoing, reached, err, model_map, env_keys)
        plan = compiler.compile()
        endpoint_warnings.extend(compiler.warnings)
        if plan is None:
            continue
        key = (plan.method, re.sub(PATH_PARAM_RE, "{}", plan.path))
        if key in routes_seen:
            err(nid, f"маршрут {plan.method} {plan.path} уже объявлен другим эндпоинтом")
            continue
        routes_seen[key] = nid
        plans.append(plan)

    errors = [i for i in issues if i.level == "error"]
    if errors:
        raise GraphError(errors)

    warnings = endpoint_warnings + [
        Issue(nid, "блок не подключён ни к одному эндпоинту — он не будет выполняться", "warning")
        for nid in nodes if nid not in reached
    ]
    if not any(n.get("type") == "endpoint" for n in nodes.values()):
        warnings.append(Issue(None, "в проекте нет ни одного HTTP-эндпоинта", "warning"))
    used_models = {s.model for p in plans for s in iter_steps(p.chain) if isinstance(s, DbStep)}
    for m in models:
        if m.name not in used_models:
            warnings.append(Issue(None, f"модель «{m.name}» не используется ни одним блоком БД", "warning"))
    return CompiledProject(str(graph.get("name") or ""), plans, models, env, warnings,
                           sandbox_settings, packages, bool(libraries.get("only_binary", True)), firewall)


def compile_graph(graph: dict) -> tuple[list[EndpointPlan], list[Issue]]:
    """Совместимость: только планы и предупреждения."""
    project = compile_project(graph)
    return project.plans, project.warnings


class _EndpointCompiler:
    def __init__(self, nid, nodes, outgoing, reached, err, models, env_keys):
        self.nid, self.nodes, self.outgoing, self.reached = nid, nodes, outgoing, reached
        self.err, self.models, self.env_keys = err, models, env_keys
        self.ok = True
        self.visited: set[str] = set()
        self.inputs: list[InputStep] = []
        self.auths: list[AuthStep] = []
        self.param_names: set[str] = set()  # все поля запроса эндпоинта — это параметры обработчика
        self.declared_path: set[str] = set()
        self.has_json = self.has_form = False
        self.warnings: list[Issue] = []

    def fail(self, node_id, msg):
        self.ok = False
        self.err(node_id, msg)

    def compile(self) -> EndpointPlan | None:
        data = self.nodes[self.nid].get("data") or {}
        self.method = str(data.get("method", "")).upper()
        self.path = str(data.get("path", "")).strip()
        if self.method not in HTTP_METHODS:
            self.fail(self.nid, f"неизвестный HTTP-метод «{self.method}»")
        if not PATH_RE.match(self.path):
            self.fail(self.nid, f"некорректный путь «{self.path}»: должен начинаться с / и содержать "
                                f"латиницу, цифры, -, _, {{param}}")
        self.path_params = PATH_PARAM_RE.findall(self.path)
        for p in self.path_params:
            if msg := _check_ident(p, "параметр пути"):
                self.fail(self.nid, msg)
        self.param_names.update(self.path_params)

        self.visited.add(self.nid)
        self.reached.add(self.nid)
        first = self.outgoing.get((self.nid, None))
        if first is None:
            self.fail(self.nid, "эндпоинт ни к чему не подключён: цепочка должна заканчиваться блоком «Ответ»")
            return None
        chain = self._walk(first, self.nid, {p: "str" for p in self.path_params})

        if self.has_json and self.has_form:
            self.fail(self.nid, "нельзя смешивать JSON-тело и form-data в одном эндпоинте")
        if not self.ok or chain is None:
            return None
        return EndpointPlan(self.nid, self.method, self.path, str(data.get("summary") or ""), chain,
                            self.path_params, self.inputs, self.auths)

    def _walk(self, cur: str | None, prev: str, known: dict[str, str]) -> Chain | None:
        steps: list[Step] = []
        while True:
            if cur is None:
                self.fail(prev, "цепочка обрывается: она должна заканчиваться блоком «Ответ»")
                return None
            if cur in self.visited:
                self.fail(cur, "цикл в цепочке блоков")
                return None
            self.visited.add(cur)
            self.reached.add(cur)
            node = self.nodes[cur]
            ntype, ndata = node.get("type"), node.get("data") or {}

            if ntype == "response":
                resp = self._response(cur, ndata, known)
                return Chain(steps, resp) if resp else None

            if ntype == "condition":
                cond = self._assignments(cur, [{"target": "condition", "expr": ndata.get("expr", "")}],
                                         known, "target", keys_are_json=True)
                then_id = self.outgoing.get((cur, "true"))
                else_id = self.outgoing.get((cur, "false"))
                if then_id is None or else_id is None:
                    self.fail(cur, "у условия должны быть подключены оба выхода: «да» и «нет»")
                    return None
                then = self._walk(then_id, cur, dict(known))
                otherwise = self._walk(else_id, cur, dict(known))
                if not cond or then is None or otherwise is None:
                    return None
                return Chain(steps, Branch(cur, cond[0], then, otherwise))

            step = {
                "input": self._input, "validate": self._validate, "logic": self._logic,
                "auth": self._auth, "db": self._db, "code": self._code,
            }.get(ntype, lambda *a: None)(cur, ndata, known)
            if step is not None:
                steps.append(step)
            prev, cur = cur, self.outgoing.get((cur, None))

    # --- блоки ---

    def _input(self, cur, ndata, known):
        source = ndata.get("source")
        if source not in INPUT_SOURCES:
            self.fail(cur, f"неизвестный источник данных «{source}»")
            source = "query"
        fields: list[FieldSpec] = []
        raw_fields = ndata.get("fields") or []
        if not raw_fields:
            self.fail(cur, "в блоке нет ни одного поля")
        for f in raw_fields:
            name, ftype = f.get("name"), f.get("type", "str")
            if msg := _check_ident(name, "поле"):
                self.fail(cur, msg)
                continue
            if ftype not in FIELD_TYPES:
                self.fail(cur, f"поле «{name}»: неизвестный тип {ftype}")
                continue
            if ftype == "file" and source != "form":
                self.fail(cur, f"поле «{name}»: файлы принимаются только из form-data")
                continue
            redeclare_path = source == "path" and name in self.path_params and name not in self.declared_path
            if (name in self.param_names or name in known) and not redeclare_path:
                self.fail(cur, f"поле «{name}» уже объявлено в этом эндпоинте")
                continue
            if source == "path":
                if name not in self.path_params:
                    self.fail(cur, f"поля «{name}» нет в пути {self.path}")
                    continue
                self.declared_path.add(name)
            required = bool(f.get("required", True)) or source == "path"
            try:
                default = None if required or ftype == "file" else _coerce_default(f.get("default"), ftype)
            except ValueError as e:
                self.fail(cur, f"поле «{name}»: {e}")
                continue
            fields.append(FieldSpec(name, ftype, required, default))
            known[name] = ftype
            self.param_names.add(name)
        if source == "json":
            self.has_json = True
            if self.method in ("GET", "DELETE"):
                self.fail(cur, f"{self.method}-запрос не принимает JSON-тело — используйте query или path")
        if source == "form":
            self.has_form = True
        step = InputStep(cur, source, fields)
        self.inputs.append(step)
        return step

    def _validate(self, cur, ndata, known):
        rules: list[Rule] = []
        for r in ndata.get("rules") or []:
            fname, op, raw = r.get("field"), r.get("op"), r.get("value")
            if fname not in known:
                self.fail(cur, f"правило ссылается на неизвестное поле «{fname}»")
                continue
            if op not in VALIDATION_OPS:
                self.fail(cur, f"неизвестное правило «{op}»")
                continue
            ftype = known[fname]
            if op in ("min", "max") and ftype not in ("int", "float", "any"):
                self.fail(cur, f"правило {op} применимо только к числам, а «{fname}» — {ftype}")
                continue
            if op in ("min_len", "max_len") and ftype not in ("str", "any"):
                self.fail(cur, f"правило {op} применимо только к строкам, а «{fname}» — {ftype}")
                continue
            try:
                if op in ("min", "max"):
                    value = float(raw)
                elif op in ("min_len", "max_len"):
                    value = int(raw)
                elif op == "regex":
                    re.compile(str(raw))
                    value = str(raw)
                else:
                    value = [v.strip() for v in str(raw).split(",") if v.strip()]
            except (TypeError, ValueError, re.error) as e:
                self.fail(cur, f"правило {fname} {op}: некорректное значение ({e})")
                continue
            rules.append(Rule(fname, op, value, r.get("message") or _default_message(fname, op, raw)))
        return ValidateStep(cur, rules)

    def _logic(self, cur, ndata, known):
        assigns = self._assignments(cur, ndata.get("assignments") or [], known, "target")
        for a in assigns:
            known[a.target] = "any"
        return LogicStep(cur, assigns)

    def _auth(self, cur, ndata, known):
        scheme = ndata.get("scheme")
        name = str(ndata.get("name") or "").strip()
        secret_env = str(ndata.get("secret_env") or "").strip()
        if scheme not in AUTH_SCHEMES:
            self.fail(cur, f"неизвестная схема авторизации «{scheme}»")
            return None
        if scheme == "bearer":
            name = "Authorization"
        elif scheme == "api_key_header" and not HEADER_RE.match(name):
            self.fail(cur, f"некорректное имя заголовка «{name}»")
            return None
        elif scheme == "api_key_query" and (msg := _check_ident(name, "query-параметр")):
            self.fail(cur, msg)
            return None
        if secret_env not in self.env_keys:
            self.fail(cur, f"переменная окружения «{secret_env}» не объявлена — добавьте её в настройках проекта")
            return None
        step = AuthStep(cur, scheme, name, secret_env, param=f"auth_{len(self.auths)}")
        self.auths.append(step)
        return step

    def _db(self, cur, ndata, known):
        model = self.models.get(ndata.get("model"))
        op = ndata.get("op")
        target = ndata.get("target")
        if model is None:
            self.fail(cur, f"модель «{ndata.get('model') or '—'}» не найдена — создайте её в «Моделях данных»"
                      if self.models else "в проекте нет моделей данных — создайте модель в «Моделях данных»")
            return None
        if op not in DB_OPS:
            self.fail(cur, f"неизвестная операция «{op}»")
            return None
        if msg := _check_ident(target, "результат"):
            self.fail(cur, msg)
            return None
        step = DbStep(cur, model.name, op, target)
        if op in ("get", "update", "delete"):
            ids = self._assignments(cur, [{"target": "id", "expr": ndata.get("id_expr", "")}], known, "target",
                                    keys_are_json=True)
            if not ids:
                return None
            step.id_value = ids[0]
        if op in ("insert", "update"):
            step.values = self._model_fields(cur, model, ndata.get("values") or [], known, "значение")
            if op == "insert":
                missing = [f["name"] for f in model.fields if f["name"] not in {a.target for a in step.values}]
                if missing:
                    self.err_warn(cur, f"при создании не заданы поля {', '.join(missing)} — будут пустыми")
        if op == "list":
            step.filters = self._model_fields(cur, model, ndata.get("filters") or [], known, "фильтр")
            try:
                step.limit = int(ndata.get("limit") or 100)
                if not 1 <= step.limit <= 1000:
                    raise ValueError
            except (TypeError, ValueError):
                self.fail(cur, "лимит должен быть целым числом от 1 до 1000")
            order_by = ndata.get("order_by") or "id"
            if order_by != "id" and order_by not in model.field_names:
                self.fail(cur, f"сортировка по неизвестному полю «{order_by}»")
            step.order_by = order_by
            step.descending = bool(ndata.get("descending"))
        known[target] = "any"
        return step

    def _code(self, cur, ndata, known):
        code = str(ndata.get("code") or "")
        if len(code) > MAX_CODE_LEN:
            self.fail(cur, f"код длиннее {MAX_CODE_LEN} символов")
            return None
        try:
            tree = ast.parse(code, "<code>")
        except SyntaxError as e:
            self.fail(cur, f"синтаксическая ошибка в строке {e.lineno}: {e.msg}")
            return None
        run = next((n for n in tree.body if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef))
                    and n.name == "run"), None)
        if run is None:
            self.fail(cur, "в коде должна быть функция run(data, env)")
            return None
        if isinstance(run, ast.AsyncFunctionDef):
            self.fail(cur, "run() должна быть обычной функцией, не async")
            return None
        if len(run.args.args) != 2:
            self.fail(cur, "сигнатура должна быть ровно run(data, env)")
            return None
        outputs: list[str] = []
        for name in ndata.get("outputs") or []:
            if msg := _check_ident(name, "выходное поле"):
                self.fail(cur, msg)
                continue
            if name in outputs:
                self.fail(cur, f"выходное поле «{name}» объявлено дважды")
                continue
            outputs.append(name)
        if not outputs:
            self.err_warn(cur, "не объявлено ни одного выходного поля — результат run() не будет использован")
        try:
            timeout = float(ndata.get("timeout") or 2)
            memory = int(ndata.get("memory_mb") or 256)
            if not (0.1 <= timeout <= 10 and 32 <= memory <= 1024):
                raise ValueError
        except (TypeError, ValueError):
            self.fail(cur, "лимиты: время 0.1–10 с, память 32–1024 МБ")
            return None
        for name in outputs:
            known[name] = "any"
        return CodeStep(cur, code, outputs, timeout, memory)

    def err_warn(self, node_id, msg):
        self.warnings.append(Issue(node_id, msg, "warning"))

    def _model_fields(self, cur, model, items, known, what):
        out = []
        for it in items:
            if it.get("field") not in model.field_names:
                self.fail(cur, f"{what}: у модели {model.name} нет поля «{it.get('field')}»")
                continue
            out += self._assignments(cur, [{"target": it["field"], "expr": it.get("expr", "")}], known,
                                     "target", keys_are_json=True)
        return out

    def _response(self, cur, ndata, known):
        try:
            status = int(ndata.get("status", 200))
            if not 100 <= status <= 599:
                raise ValueError
        except (TypeError, ValueError):
            self.fail(cur, f"некорректный статус ответа «{ndata.get('status')}»")
            return None
        body = self._assignments(cur, ndata.get("body") or [], known, "key", keys_are_json=True)
        if any(t for (s, _), t in self.outgoing.items() if s == cur):
            self.fail(cur, "после «Ответа» не может быть других блоков")
        return ResponseSpec(cur, status, body)

    def _assignments(self, node_id, items, known, key_name, keys_are_json=False) -> list[Assignment]:
        out: list[Assignment] = []
        local_known = dict(known)
        for item in items:
            target = item.get(key_name)
            if keys_are_json:
                if not isinstance(target, str) or not target.strip():
                    self.fail(node_id, "пустой ключ")
                    continue
            elif msg := _check_ident(target, "поле"):
                self.fail(node_id, msg)
                continue
            source = str(item.get("expr", ""))
            try:
                tree = expr.parse(source)
            except expr.ExprError as e:
                self.fail(node_id, f"{target} = {source!r}: {e}")
                continue
            missing = expr.names(tree) - set(local_known) - {expr.ENV_NAME}
            if missing:
                self.fail(node_id, f"{target} = {source!r}: неизвестные поля {', '.join(sorted(missing))}. "
                                   f"Доступны: {', '.join(sorted(local_known)) or 'нет полей'}")
                continue
            out.append(Assignment(target, source, tree))
            if not keys_are_json:
                local_known[target] = "any"
        return out
