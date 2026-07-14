"""Pure-logic tests for person expertise summaries (#42).

The prompt building, the deterministic fallback rendering, and the source
extraction run without a DB or an LLM, so they are unit-tested here. The graph
read/write + job runner are covered in test_person_expertise_graph.py (skips
without Neo4j). Fictional people/companies only (public-repo rule).
"""

from app.graph.person_expertise import (
    build_expertise_prompt,
    expertise_sources,
    relation_label,
    render_expertise_fallback,
)

CTX_FULL = {
    "name": "Ada Placeholder",
    "currentRoles": [{"company": "Acme Labs", "title": "CTO"}],
    "priorRoles": [{"company": "Globex", "title": "Engineer", "from": 2010, "to": 2015}],
    "signals": [
        {
            "title": "Scaling graph databases",
            "kind": "blog",
            "relation": "AUTHORED",
            "url": "https://acme.example/post",
            "when": "2024-01-02",
        },
        {
            "title": "Keynote on agents",
            "kind": "event",
            "relation": "SPOKE_AT",
            "url": "https://conf.example/talk",
            "when": "2023-06-01",
        },
        {
            "title": "Quoted on tooling",
            "kind": "news",
            "relation": "QUOTED_IN",
            "url": None,
            "when": None,
        },
        {
            "title": "Duplicate url post",
            "kind": "blog",
            "relation": "AUTHORED",
            "url": "https://acme.example/post",
            "when": None,
        },
        {
            "title": "Non-http source",
            "kind": "blog",
            "relation": "AUTHORED",
            "url": "ftp://acme.example/file",
            "when": None,
        },
    ],
}

EMPTY = {"name": "Nemo Placeholder", "currentRoles": [], "priorRoles": [], "signals": []}


def test_relation_label():
    assert relation_label("AUTHORED") == "authored"
    assert relation_label("SPOKE_AT") == "spoke at"
    assert relation_label("QUOTED_IN") == "quoted in"
    # An unexpected relation degrades to a readable lowercase form, never a crash.
    assert relation_label("SOME_REL") == "some rel"


def test_expertise_sources_http_only_and_deduped():
    # Only http(s) URLs count as citable sources; duplicates collapse; order kept.
    assert expertise_sources(CTX_FULL) == [
        "https://acme.example/post",
        "https://conf.example/talk",
    ]


def test_expertise_sources_empty_when_no_linked_signals():
    assert expertise_sources(EMPTY) == []


def test_fallback_mentions_name_role_and_signal_count():
    text = render_expertise_fallback(CTX_FULL)
    assert "Ada Placeholder" in text
    assert "CTO" in text and "Acme Labs" in text
    assert "5" in text  # five linked signals
    # A conservative, deterministic sentence — never empty.
    assert text.endswith(".")


def test_fallback_empty_context_is_explicit():
    text = render_expertise_fallback(EMPTY)
    assert "Nemo Placeholder" in text
    assert "no" in text.lower()


def test_prompt_is_grounded_and_injection_guarded():
    prompt = build_expertise_prompt(CTX_FULL)
    # Grounded in graph facts: the person, a role company, and a signal title.
    assert "Ada Placeholder" in prompt
    assert "Acme Labs" in prompt
    assert "Scaling graph databases" in prompt
    # Untrusted crawled titles must be framed as data, never as instructions.
    assert "instruction" in prompt.lower()


def test_prompt_omits_urls():
    # The prompt feeds titles/roles only — never the raw source URLs (kept for the
    # stored `expertiseSources` list, not the model input).
    prompt = build_expertise_prompt(CTX_FULL)
    assert "https://acme.example/post" not in prompt
