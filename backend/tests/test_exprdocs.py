"""Каждый пример из справки «?» считается настоящим вычислителем и должен дать заявленный результат."""

import pytest

from app import expr
from app.exprdocs import FUNCTIONS, MISTAKES, SAMPLE, SAMPLE_ENV, SECTIONS
from dragapi_agent.runner import render_template

CASES = [(s["id"], ex) for s in SECTIONS for ex in s["examples"]] + [("functions", f) for f in FUNCTIONS]


@pytest.mark.parametrize("section,ex", CASES, ids=[f"{s}:{e.get('expr') or e.get('template')}" for s, e in CASES])
def test_doc_example(section, ex):
    if "template" in ex:
        got = render_template(ex["template"], SAMPLE, SAMPLE_ENV)
    else:
        got = expr.evaluate(expr.parse(ex["expr"]), SAMPLE, SAMPLE_ENV)
    assert got == ex["result"] and type(got) is type(ex["result"]), (ex, got)


@pytest.mark.parametrize("m", MISTAKES, ids=[m["wrong"] for m in MISTAKES])
def test_mistakes_right_variant_is_valid(m):
    expr.parse(m["right"])     # «как правильно» — всегда корректное выражение


def test_doc_endpoint():
    from fastapi.testclient import TestClient
    from app.main import app
    d = TestClient(app).get("/api/expr/docs").json()
    assert d["sections"][0]["id"] == "basics" and d["functions"] and d["sample"]["age"] == 25
