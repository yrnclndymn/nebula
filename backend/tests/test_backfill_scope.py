"""Structured back-fill scope filter → parameterized Cypher (pure, no DB).

The scope is how a back-fill picks which companies get the field filled. It must
be a validated structured filter (allowlisted field + operator, parameterized
value), never free-text Cypher from the model — untrusted input may steer reads
but a back-fill leads to a WRITE, so its scope has to be deterministic and
injection-safe.
"""

import pytest

from app.agents.assistant.backfill import parse_scope, scope_to_cypher


def test_parse_empty_is_no_conditions():
    assert parse_scope(None) == []
    assert parse_scope("") == []
    assert parse_scope([]) == []


def test_parse_single_numeric_condition():
    out = parse_scope([{"field": "headcount", "op": ">", "value": 200}])
    assert out == [{"field": "headcount", "op": ">", "value": 200}]


def test_parse_accepts_json_string():
    out = parse_scope('[{"field": "headcount", "op": ">=", "value": 200}]')
    assert out == [{"field": "headcount", "op": ">=", "value": 200}]


def test_parse_coerces_numeric_string_value():
    # A model may emit the value as a string; numeric fields coerce it.
    out = parse_scope([{"field": "headcount", "op": ">", "value": "200"}])
    assert out == [{"field": "headcount", "op": ">", "value": 200}]
    assert isinstance(out[0]["value"], int)


def test_parse_multiple_conditions_compose():
    out = parse_scope(
        [
            {"field": "headcount", "op": ">", "value": 200},
            {"field": "hqCountry", "op": "=", "value": "United Kingdom"},
        ]
    )
    assert len(out) == 2


def test_parse_null_ops_need_no_value():
    out = parse_scope([{"field": "funding", "op": "is_not_null"}])
    assert out == [{"field": "funding", "op": "is_not_null", "value": None}]


# --- injection safety: hostile field / operator must be rejected -------------


def test_rejects_unknown_field():
    with pytest.raises(ValueError, match="field"):
        parse_scope([{"field": "junk", "op": "=", "value": 1}])


def test_rejects_unknown_operator():
    with pytest.raises(ValueError, match="operator"):
        parse_scope([{"field": "headcount", "op": "BURN", "value": 1}])


def test_rejects_cypher_injection_in_field():
    # A field carrying Cypher must never be accepted — it isn't on the allowlist.
    hostile = "headcount} SET c.pwned = true //"
    with pytest.raises(ValueError, match="field"):
        parse_scope([{"field": hostile, "op": ">", "value": 1}])


def test_rejects_cypher_injection_in_operator():
    with pytest.raises(ValueError, match="operator"):
        parse_scope([{"field": "headcount", "op": "> 0 OR 1=1 //", "value": 1}])


def test_rejects_non_numeric_value_for_numeric_field():
    with pytest.raises(ValueError, match="numeric"):
        parse_scope([{"field": "headcount", "op": ">", "value": "lots"}])


def test_rejects_missing_value_for_value_op():
    with pytest.raises(ValueError, match="value"):
        parse_scope([{"field": "headcount", "op": ">"}])


def test_rejects_non_list():
    with pytest.raises(ValueError):
        parse_scope('{"field": "headcount"}')


def test_rejects_malformed_json():
    with pytest.raises(ValueError):
        parse_scope("not json")


# --- translation is fully parameterized -------------------------------------


def test_cypher_is_parameterized_no_literals():
    conds = parse_scope([{"field": "headcount", "op": ">", "value": 200}])
    clause, params = scope_to_cypher(conds)
    # The value never appears literally in the Cypher — only as a $param.
    assert "200" not in clause
    # The field name is passed as a parameter too (dynamic property access),
    # so an allowlisted-but-still-untrusted string can't reshape the query.
    assert "headcount" not in clause
    assert 200 in params.values()
    assert "headcount" in params.values()
    assert clause  # non-empty


def test_cypher_uses_mapped_operator():
    conds = parse_scope([{"field": "headcount", "op": "!=", "value": 5}])
    clause, _ = scope_to_cypher(conds)
    assert "<>" in clause  # mapped, not the raw "!=" token


def test_cypher_null_op_has_no_value_param():
    conds = parse_scope([{"field": "funding", "op": "is_null"}])
    clause, params = scope_to_cypher(conds)
    assert "IS NULL" in clause
    # Only the field param exists; no value param for a null check.
    assert "funding" in params.values()
    assert len(params) == 1


def test_cypher_contains_is_case_insensitive():
    conds = parse_scope([{"field": "hqCountry", "op": "contains", "value": "king"}])
    clause, params = scope_to_cypher(conds)
    assert "CONTAINS" in clause
    assert "toLower" in clause
    assert "king" in params.values()


def test_cypher_multiple_conditions_anded_with_distinct_params():
    conds = parse_scope(
        [
            {"field": "headcount", "op": ">", "value": 200},
            {"field": "yearFounded", "op": "<", "value": 2010},
        ]
    )
    clause, params = scope_to_cypher(conds)
    assert " AND " in clause
    # Distinct param names per condition — no clobbering.
    assert 200 in params.values()
    assert 2010 in params.values()
    assert len([v for v in params.values() if v in (200, 2010)]) == 2
