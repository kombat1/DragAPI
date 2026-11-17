"""Безопасные выражения для блоков «Логика» и «Ответ».

Это не полноценный Python: разрешён небольшой белый список AST-узлов
и функций. Тот же разобранный AST используется и в интерпретаторе
(evaluate), и в генераторе кода (to_python), поэтому превью и экспорт
ведут себя одинаково.
"""

from __future__ import annotations

import ast
import re
from collections import ChainMap
from typing import Any


class ExprError(ValueError):
    pass


def _safe_pow(a, b):
    if isinstance(b, (int, float)) and abs(b) > 100:
        raise ExprError("слишком большая степень")
    return a**b


FUNCTIONS: dict[str, Any] = {
    "len": len,
    "str": str,
    "int": int,
    "float": float,
    "bool": bool,
    "abs": abs,
    "min": min,
    "max": max,
    "sum": sum,
    "round": round,
    "upper": lambda s: str(s).upper(),
    "lower": lambda s: str(s).lower(),
    "strip": lambda s: str(s).strip(),
    "contains": lambda container, item: item in container,
    "default": lambda value, fallback: fallback if value is None else value,
    "regex_match": lambda pattern, s: re.fullmatch(pattern, str(s)) is not None,
}

# Исходники тех же функций для экспортированного кода.
FUNCTIONS_SOURCE = '''
def upper(s): return str(s).upper()
def lower(s): return str(s).lower()
def strip(s): return str(s).strip()
def contains(container, item): return item in container
def default(value, fallback): return fallback if value is None else value
def regex_match(pattern, s): return re.fullmatch(pattern, str(s)) is not None
'''.strip()

_ALLOWED_NODES = (
    ast.Expression, ast.BinOp, ast.UnaryOp, ast.BoolOp, ast.Compare, ast.IfExp,
    ast.Name, ast.Load, ast.Constant, ast.Call, ast.Subscript, ast.Slice,
    ast.List, ast.Tuple, ast.Dict, ast.JoinedStr, ast.FormattedValue,
    ast.Add, ast.Sub, ast.Mult, ast.Div, ast.FloorDiv, ast.Mod, ast.Pow,
    ast.USub, ast.UAdd, ast.Not, ast.And, ast.Or,
    ast.Eq, ast.NotEq, ast.Lt, ast.LtE, ast.Gt, ast.GtE, ast.In, ast.NotIn,
    ast.Is, ast.IsNot,
)
_CONSTANT_NAMES = {"True", "False", "None"}
# Переменные окружения проекта: доступны в выражениях как env['KEY'],
# но не лежат в контексте — не попадают в ответы «весь контекст» и в логи.
ENV_NAME = "env"
MAX_EXPR_LEN = 500
MAX_STR_LEN = 100_000


def parse(source: str) -> ast.Expression:
    if not isinstance(source, str) or not source.strip():
        raise ExprError("пустое выражение")
    if len(source) > MAX_EXPR_LEN:
        raise ExprError(f"выражение длиннее {MAX_EXPR_LEN} символов")
    try:
        tree = ast.parse(source.strip(), mode="eval")
    except SyntaxError as e:
        raise ExprError(f"синтаксическая ошибка: {e.msg}") from None
    for node in ast.walk(tree):
        if not isinstance(node, _ALLOWED_NODES):
            raise ExprError(f"недопустимая конструкция: {type(node).__name__}")
        if isinstance(node, ast.Call):
            if not isinstance(node.func, ast.Name) or node.func.id not in FUNCTIONS:
                raise ExprError("можно вызывать только функции: " + ", ".join(sorted(FUNCTIONS)))
            if node.keywords:
                raise ExprError("именованные аргументы не поддерживаются")
    return tree


def names(tree: ast.Expression) -> set[str]:
    """Переменные контекста, которые читает выражение."""
    called = {n.func.id for n in ast.walk(tree) if isinstance(n, ast.Call)}
    return {
        n.id for n in ast.walk(tree)
        if isinstance(n, ast.Name) and n.id not in _CONSTANT_NAMES and n.id not in called
    }


class _Evaluator:
    def __init__(self, ctx: dict[str, Any]):
        self.ctx = ctx

    def eval(self, node: ast.AST) -> Any:
        method = getattr(self, "_" + type(node).__name__)
        return method(node)

    def _Expression(self, n): return self.eval(n.body)
    def _Constant(self, n): return n.value
    def _List(self, n): return [self.eval(e) for e in n.elts]
    def _Tuple(self, n): return tuple(self.eval(e) for e in n.elts)
    def _Dict(self, n): return {self.eval(k): self.eval(v) for k, v in zip(n.keys, n.values)}

    def _Name(self, n):
        if n.id in _CONSTANT_NAMES:
            return {"True": True, "False": False, "None": None}[n.id]
        if n.id not in self.ctx:
            raise ExprError(f"неизвестное поле «{n.id}»")
        return self.ctx[n.id]

    def _BinOp(self, n):
        a, b = self.eval(n.left), self.eval(n.right)
        op = type(n.op)
        if op is ast.Pow:
            return _safe_pow(a, b)
        if op is ast.Mult and (isinstance(a, (str, list)) or isinstance(b, (str, list))):
            count = b if isinstance(a, (str, list)) else a
            seq = a if isinstance(a, (str, list)) else b
            if isinstance(count, int) and len(seq) * count > MAX_STR_LEN:
                raise ExprError("слишком длинный результат")
        return {
            ast.Add: lambda: a + b, ast.Sub: lambda: a - b, ast.Mult: lambda: a * b,
            ast.Div: lambda: a / b, ast.FloorDiv: lambda: a // b, ast.Mod: lambda: a % b,
        }[op]()

    def _UnaryOp(self, n):
        v = self.eval(n.operand)
        return {ast.USub: lambda: -v, ast.UAdd: lambda: +v, ast.Not: lambda: not v}[type(n.op)]()

    def _BoolOp(self, n):
        if isinstance(n.op, ast.And):
            result = True
            for v in n.values:
                result = self.eval(v)
                if not result:
                    return result
            return result
        result = False
        for v in n.values:
            result = self.eval(v)
            if result:
                return result
        return result

    def _Compare(self, n):
        left = self.eval(n.left)
        for op, comp in zip(n.ops, n.comparators):
            right = self.eval(comp)
            ok = {
                ast.Eq: lambda: left == right, ast.NotEq: lambda: left != right,
                ast.Lt: lambda: left < right, ast.LtE: lambda: left <= right,
                ast.Gt: lambda: left > right, ast.GtE: lambda: left >= right,
                ast.In: lambda: left in right, ast.NotIn: lambda: left not in right,
                ast.Is: lambda: left is right, ast.IsNot: lambda: left is not right,
            }[type(op)]()
            if not ok:
                return False
            left = right
        return True

    def _IfExp(self, n):
        return self.eval(n.body) if self.eval(n.test) else self.eval(n.orelse)

    def _Call(self, n):
        return FUNCTIONS[n.func.id](*[self.eval(a) for a in n.args])

    def _Subscript(self, n):
        return self.eval(n.value)[self.eval(n.slice)]

    def _Slice(self, n):
        return slice(
            self.eval(n.lower) if n.lower else None,
            self.eval(n.upper) if n.upper else None,
            self.eval(n.step) if n.step else None,
        )

    def _JoinedStr(self, n):
        return "".join(str(self.eval(v)) for v in n.values)

    def _FormattedValue(self, n):
        value = self.eval(n.value)
        if n.conversion == ord("r"):
            value = repr(value)
        elif n.conversion == ord("s"):
            value = str(value)
        spec = self.eval(n.format_spec) if n.format_spec else ""
        if re.search(r"\d{4,}", spec):
            raise ExprError("слишком большая ширина в формате")
        return format(value, spec)


def evaluate(tree: ast.Expression, ctx: dict[str, Any], env: dict[str, str] | None = None) -> Any:
    scope = ChainMap(ctx, {ENV_NAME: dict(env or {})})
    try:
        return _Evaluator(scope).eval(tree)
    except ExprError:
        raise
    except Exception as e:  # ошибки пользовательских данных: деление на 0, тип и т.п.
        raise ExprError(f"{type(e).__name__}: {e}") from None


class _CtxRewriter(ast.NodeTransformer):
    """name -> ctx["name"] для генерации кода.

    f-строки разворачиваются в 'a' + format(x, '') + ...: иначе ast.unparse на 3.12
    выдаёт f'{ctx['x']}', а такие кавычки не разбираются Python 3.10/3.11.
    """

    def visit_JoinedStr(self, node):
        parts: list[ast.expr] = []
        for v in node.values:
            if isinstance(v, ast.FormattedValue):
                value = self.visit(v.value)
                conv = {ord("r"): "repr", ord("s"): "str", ord("a"): "ascii"}.get(v.conversion)
                if conv:
                    value = ast.Call(ast.Name(conv, ast.Load()), [value], [])
                spec = self.visit(v.format_spec) if v.format_spec else ast.Constant("")
                parts.append(ast.Call(ast.Name("format", ast.Load()), [value, spec], []))
            else:
                parts.append(v)
        if not parts:
            return ast.Constant("")
        result = parts[0]
        for p in parts[1:]:
            result = ast.BinOp(result, ast.Add(), p)
        return result

    def visit_Call(self, node):
        node.args = [self.visit(a) for a in node.args]
        return node

    def visit_Name(self, node):
        if node.id in _CONSTANT_NAMES:
            return node
        if node.id == ENV_NAME:
            return ast.Name("ENV", ast.Load())
        return ast.Subscript(value=ast.Name("ctx", ast.Load()), slice=ast.Constant(node.id), ctx=ast.Load())


def to_python(tree: ast.Expression) -> str:
    import copy
    rewritten = _CtxRewriter().visit(copy.deepcopy(tree))
    return ast.unparse(ast.fix_missing_locations(rewritten))
