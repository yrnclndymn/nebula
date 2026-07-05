"""Agent wiring + tool-helper tests (no network / no model calls)."""

from app.tools.graph_tools import _parse_leaders


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
