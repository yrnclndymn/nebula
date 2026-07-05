"""Agent wiring + tool-helper tests (no network / no model calls)."""

from app.graph.models import Citation
from app.tools.graph_tools import _drop_uncited, _parse_citations, _parse_leaders


def test_root_agent_wires_up_with_tools():
    from app.agents.enrichment.agent import root_agent

    assert root_agent.name == "enrichment_agent"
    tool_names = {getattr(t, "__name__", getattr(t, "name", None)) for t in root_agent.tools}
    assert {"web_search", "fetch_page", "save_company"} <= tool_names


def test_parse_leaders_handles_name_title_and_bare_names():
    leaders = _parse_leaders(["Jane Roe | CEO", "John Doe", "  | ", "Amy Ng | CTO"])
    assert [(le.name, le.title) for le in leaders] == [
        ("Jane Roe", "CEO"),
        ("John Doe", None),
        ("Amy Ng", "CTO"),
    ]


def test_parse_citations_finds_url_by_content():
    cites = _parse_citations(
        [
            "funding | $250M Series C | https://example.com/news | 2025-09",
            "year_founded | 2021 | https://example.com/about",  # no date
            "leadership | Ben Mann | Co-Founder | https://example.com/team",  # extra pipe
            "headcount | 500 | not-a-url",  # no URL → dropped
        ]
    )
    assert [(c.field, c.source, c.source_date) for c in cites] == [
        ("funding", "https://example.com/news", "2025-09"),
        ("year_founded", "https://example.com/about", None),
        ("leadership", "https://example.com/team", "Co-Founder"),
    ]


def test_drop_uncited_removes_unbacked_financials():
    values = {"funding": "$40M", "estimated_revenue": "$5M", "headcount": 200}
    citations = [
        Citation(field="funding", value="$40M", source="https://x.com"),
        # "revenue" is an accepted alias for estimated_revenue
        Citation(field="revenue", value="$5M", source="https://y.com"),
        # headcount has no citation → dropped
    ]
    kept, dropped = _drop_uncited(values, citations)
    assert kept == {"funding": "$40M", "estimated_revenue": "$5M", "headcount": None}
    assert dropped == ["headcount"]
