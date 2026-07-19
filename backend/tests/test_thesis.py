"""Acquisition-thesis model + seed (#193, epic #192): the PURE shaping/validation.

The rule model and its identity key run without a DB or network, so they are the
real guardrail under test here: a rule's identity is stable and qualifier-scoped,
kinds are normalised, confidence is bounded, and the maintainer's three seed rules
encode exactly the stated thesis with origin='user'. Graph round-trip lives in
test_thesis_graph.py. Fictional/abstract kinds only (public-repo rule).
"""

import pytest
from pydantic import ValidationError

from app.graph.thesis import SEED_RULES, ThesisRule


def _rule(**kw) -> ThesisRule:
    base = dict(
        acquirer_kind="cloud_provider",
        target_kind="service_provider",
        statement="Cloud providers acquire services companies.",
    )
    base.update(kw)
    return ThesisRule(**base)


# --- identity key: stable + qualifier-scoped (pure) --------------------------


def test_rule_key_is_deterministic():
    assert _rule().rule_key == _rule().rule_key


def test_rule_key_encodes_direction_and_qualifier():
    unqualified = _rule(acquirer_kind="service_provider", target_kind="isv")
    qualified = _rule(
        acquirer_kind="service_provider", target_kind="isv", qualifier="domain-focused"
    )
    # Same kinds, different qualifier => distinct rules (distinct keys).
    assert unqualified.rule_key != qualified.rule_key
    # Direction matters: acquirer→target is not the same rule as its reverse.
    assert (
        _rule().rule_key
        != _rule(acquirer_kind="service_provider", target_kind="cloud_provider").rule_key
    )


# --- normalisation (pure) ----------------------------------------------------


def test_kinds_are_normalised_to_snake_lower():
    r = _rule(acquirer_kind="  Cloud Provider ", target_kind="Service Provider")
    assert r.acquirer_kind == "cloud_provider"
    assert r.target_kind == "service_provider"
    # Normalisation is applied before keying, so cosmetic variants share identity.
    assert r.rule_key == _rule().rule_key


def test_qualifier_defaults_empty_and_strips():
    assert _rule().qualifier == ""
    assert _rule(qualifier="  domain-focused  ").qualifier == "domain-focused"


# --- validation guards (pure) ------------------------------------------------


def test_empty_kind_is_rejected():
    with pytest.raises(ValidationError):
        _rule(acquirer_kind="   ")
    with pytest.raises(ValidationError):
        _rule(target_kind="")


def test_empty_statement_is_rejected():
    with pytest.raises(ValidationError):
        _rule(statement="   ")


def test_confidence_must_be_a_probability():
    with pytest.raises(ValidationError):
        _rule(confidence=1.5)
    with pytest.raises(ValidationError):
        _rule(confidence=-0.1)
    assert _rule(confidence=0.0).confidence == 0.0
    assert _rule(confidence=1.0).confidence == 1.0


def test_origin_is_restricted():
    with pytest.raises(ValidationError):
        _rule(origin="crawler")  # untrusted crawled content never seeds a rule
    assert _rule(origin="user").origin == "user"


# --- the maintainer's seed thesis (pure) -------------------------------------


def test_seed_encodes_exactly_the_three_stated_rules():
    keys = {(r.acquirer_kind, r.target_kind, r.qualifier) for r in SEED_RULES}
    assert keys == {
        ("cloud_provider", "service_provider", ""),
        ("service_provider", "service_provider", ""),
        ("service_provider", "isv", "domain-focused"),
    }


def test_seed_rules_are_human_authored_and_explainable():
    assert len(SEED_RULES) == 3
    for r in SEED_RULES:
        assert r.origin == "user"  # seeded by the human, not agent-written
        assert r.statement.strip()  # every rule carries a human-readable why
        assert 0.0 <= r.confidence <= 1.0
    # Rule identities are unique — the seed is a set, not a bag.
    assert len({r.rule_key for r in SEED_RULES}) == 3


def test_isv_rule_captures_the_larger_acquirer_condition():
    isv_rule = next(r for r in SEED_RULES if r.target_kind == "isv")
    assert isv_rule.qualifier == "domain-focused"
    # The "larger services companies" condition is captured in the statement text.
    assert "larger" in isv_rule.statement.lower()
