"""Acquisition-thesis model, write path, read query, and seed (#193, epic #192).

The thesis is a *top-level* model of who acquires whom in the space — stored as
DATA, not code, so it evolves without a schema migration or deploy. Each rule is a
node:

    (:ThesisRule {ruleKey, acquirerKind, targetKind, qualifier, statement,
                  confidence, updatedAt, origin})

with ``(:ThesisRule)-[:SUPPORTED_BY]->(:Source)`` edges to the provenance of the
observed ACQUIRED deals that evidence it. ``SUPPORTED_BY`` anchors on the deal's
:Source (its citation URL) rather than on the ACQUIRED edge itself — a Neo4j
relationship cannot be the endpoint of another relationship, and the Source is the
checkable provenance the thesis is meant to expose (#192: "every rule carries its
provenance"). The freshly-seeded, human-authored rules carry no evidence yet; the
evidence loop (#196) is what proposes SUPPORTED_BY edges from observed deals.

Identity is the ``(acquirerKind, targetKind, qualifier)`` triple, flattened into a
deterministic ``ruleKey`` so a MERGE dedupes a rule and a UNIQUE constraint
enforces it. Kinds are normalised (snake/lower) before keying, so cosmetic variants
never spawn a duplicate rule.

Guardrails honoured here (see repo CLAUDE.md):
- **HITL / no agent direct-write.** :func:`upsert_thesis_rule` is called ONLY by
  the seed routine below and by the future review-commit step of the evidence loop
  (#196). Research/enrichment agents never import or call it — the thesis steers
  ranking, so an agent writing it would let untrusted crawled content steer future
  suggestions. Agents propose; the reviewer commits.
- **Seeded by the human.** The initial rules encode the maintainer's stated thesis
  and are marked ``origin='user'``; ``origin`` is a restricted vocabulary so crawled
  content can never masquerade as a human-authored rule.
- **Explainable.** Every rule carries a human-readable ``statement`` and its
  evidence count, so a downstream ranking signal (#194) can cite the matched rule
  rather than emit a bare score.
"""

import asyncio

from neo4j import AsyncDriver, AsyncManagedTransaction
from pydantic import BaseModel, Field, field_validator

# Who may author a rule. Crawled/derived content is untrusted and must never
# appear here — it can only ever *propose* a revision the reviewer commits.
RULE_ORIGINS = ("user", "reviewer")


def _normalise_kind(value: str) -> str:
    """Canonical kind token: trimmed, lower-cased, spaces→underscores.

    Kinds are a small controlled vocabulary (cloud_provider, service_provider,
    isv, …); normalising here is the single choke point so ``ruleKey`` identity is
    stable across cosmetic variants ("Cloud Provider" ≡ "cloud_provider").
    """
    return "_".join(value.strip().lower().split())


class ThesisRule(BaseModel):
    """A single acquisition-thesis rule: *acquirers of kind A acquire targets of
    kind B* (optionally under a ``qualifier`` condition), with a human-readable
    ``statement`` and a ``confidence``.

    Pure/validating: kinds are normalised, the statement is required non-empty,
    ``confidence`` is a probability in ``[0, 1]``, and ``origin`` is restricted to
    :data:`RULE_ORIGINS`. Identity is :attr:`rule_key`.
    """

    acquirer_kind: str
    target_kind: str
    qualifier: str = ""  # '' when unqualified; part of the rule's identity
    statement: str
    confidence: float = Field(default=0.5, ge=0.0, le=1.0)
    origin: str = "user"

    @field_validator("acquirer_kind", "target_kind")
    @classmethod
    def _normalise_and_require_kind(cls, v: str) -> str:
        normalised = _normalise_kind(v)
        if not normalised:
            raise ValueError("kind must be non-empty")
        return normalised

    @field_validator("qualifier")
    @classmethod
    def _strip_qualifier(cls, v: str) -> str:
        return v.strip()

    @field_validator("statement")
    @classmethod
    def _require_statement(cls, v: str) -> str:
        if not v.strip():
            raise ValueError("statement must be non-empty (rules must be explainable)")
        return v.strip()

    @field_validator("origin")
    @classmethod
    def _validate_origin(cls, v: str) -> str:
        if v not in RULE_ORIGINS:
            raise ValueError(f"origin must be one of {RULE_ORIGINS}, got {v!r}")
        return v

    @property
    def rule_key(self) -> str:
        """Deterministic identity string for MERGE/uniqueness.

        Encodes direction (acquirer→target) and the qualifier, so the reverse
        direction and a differently-qualified rule are distinct rules.
        """
        base = f"{self.acquirer_kind}>{self.target_kind}"
        return f"{base}|{self.qualifier}" if self.qualifier else base


# The maintainer's stated thesis (2026-07-19), seeded as human-authored rules.
# Confidences are seed priors the evidence loop (#196) will revise from observed
# ACQUIRED deals; they are hedged to match the maintainer's own wording ("tend
# to", "sometimes"). The "larger services companies" condition of the ISV rule is
# captured in the statement text (there is no headcount predicate on the rule —
# #194's matcher reads it as guidance, and missing-kind data stays neutral, #165).
SEED_RULES: list[ThesisRule] = [
    ThesisRule(
        acquirer_kind="cloud_provider",
        target_kind="service_provider",
        statement="Cloud providers are currently acquiring services companies.",
        confidence=0.75,
        origin="user",
    ),
    ThesisRule(
        acquirer_kind="service_provider",
        target_kind="service_provider",
        statement="Services companies tend to acquire other services companies.",
        confidence=0.7,
        origin="user",
    ),
    ThesisRule(
        acquirer_kind="service_provider",
        target_kind="isv",
        qualifier="domain-focused",
        statement=(
            "Larger services companies sometimes acquire ISVs, usually where there "
            "is more of a domain focus."
        ),
        confidence=0.5,
        origin="user",
    ),
]


async def _write_rule_tx(
    tx: AsyncManagedTransaction, rule: ThesisRule, evidence: list[str]
) -> None:
    """MERGE the ThesisRule on its ``ruleKey`` and SET its facts, then MERGE a
    :Source per evidence URL and a SUPPORTED_BY edge to each. Idempotent — re-running
    neither duplicates the rule nor its provenance edges."""
    await tx.run(
        """
        MERGE (tr:ThesisRule {ruleKey: $rule_key})
        SET tr.acquirerKind = $acquirer_kind,
            tr.targetKind = $target_kind,
            tr.qualifier = $qualifier,
            tr.statement = $statement,
            tr.confidence = $confidence,
            tr.origin = $origin,
            tr.updatedAt = datetime()
        WITH tr
        UNWIND $evidence AS src_url
        MERGE (s:Source {url: src_url})
        MERGE (tr)-[:SUPPORTED_BY]->(s)
        """,
        rule_key=rule.rule_key,
        acquirer_kind=rule.acquirer_kind,
        target_kind=rule.target_kind,
        qualifier=rule.qualifier,
        statement=rule.statement,
        confidence=rule.confidence,
        origin=rule.origin,
        evidence=evidence or [],
    )


async def upsert_thesis_rule(
    driver: AsyncDriver, rule: ThesisRule, evidence: list[str] | None = None
) -> dict:
    """Write one :class:`ThesisRule` (and optional SUPPORTED_BY provenance) to the
    graph. Idempotent — MERGE on ``ruleKey``.

    Called ONLY by :func:`seed_thesis` and by the future review-commit step of the
    evidence loop (#196) — never from a research/enrichment agent directly, so the
    thesis stays human-in-the-loop (untrusted crawled content proposes, it never
    writes). ``evidence`` is a list of citation URLs (each deal's ``source``); the
    edge anchors on the :Source so the rule's support is always checkable.
    """
    async with driver.session() as session:

        async def _tx(tx: AsyncManagedTransaction) -> dict:
            await _write_rule_tx(tx, rule, evidence or [])
            return {"rule_key": rule.rule_key, "action": "written"}

        return await session.execute_write(_tx)


async def seed_thesis(driver: AsyncDriver) -> dict:
    """Idempotently write the maintainer's :data:`SEED_RULES` (origin='user').

    Safe to re-run — every rule MERGEs on its identity, so re-seeding refreshes
    ``updatedAt`` without creating duplicates. Wired to ``make seed-thesis``.
    """
    for rule in SEED_RULES:
        await upsert_thesis_rule(driver, rule)
    return {"rules": len(SEED_RULES), "action": "seeded"}


async def get_thesis_rules(driver: AsyncDriver) -> list[dict]:
    """All ThesisRules with their SUPPORTED_BY evidence count, most-confident first.

    Read-only. Feeds the thesis surface (#195) and the ranking signal (#194): each
    row carries the rule's kinds/qualifier/statement/origin plus how many observed
    deals evidence it, so a match can be shown *with* its support rather than as a
    bare score.
    """
    async with driver.session() as session:
        result = await session.run(
            """
            MATCH (tr:ThesisRule)
            RETURN tr.ruleKey AS rule_key,
                   tr.acquirerKind AS acquirer_kind,
                   tr.targetKind AS target_kind,
                   tr.qualifier AS qualifier,
                   tr.statement AS statement,
                   tr.confidence AS confidence,
                   tr.origin AS origin,
                   toString(tr.updatedAt) AS updated_at,
                   COUNT { (tr)-[:SUPPORTED_BY]->() } AS evidence_count
            ORDER BY tr.confidence DESC, tr.ruleKey
            """
        )
        return [dict(rec) async for rec in result]


async def _main() -> None:
    from app.graph.driver import close_driver, get_driver
    from app.graph.schema import apply_schema

    driver = get_driver()
    await apply_schema(driver)
    result = await seed_thesis(driver)
    print(f"Seeded {result['rules']} thesis rules (origin='user').")
    await close_driver()


if __name__ == "__main__":
    asyncio.run(_main())
