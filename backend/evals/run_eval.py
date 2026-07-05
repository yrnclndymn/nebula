"""Evaluate the enrichment agent (course Day 4).

Two phases, so you can re-grade without re-paying for agent runs:
  1. generate — run the agent on each dataset company, capture the saved record
     and the tool-call trajectory → artifacts/traces.json
  2. grade — deterministic field checks + trajectory checks + LLM-as-Judge
     (accuracy / faithfulness / completeness) → artifacts/results.json + a report

    make eval                    # generate + grade
    make eval ARGS=--grade-only  # re-grade cached traces (no agent, no web)
    make eval ARGS=--limit=1     # just the first company
"""

import argparse
import asyncio
import json
from pathlib import Path

from app.agents.enrichment.enrich import enrich
from app.graph.driver import close_driver
from evals.dataset import DATASET
from evals.judge import Judgement, judge_record

ARTIFACTS = Path(__file__).parent / "artifacts"
TRACES = ARTIFACTS / "traces.json"
RESULTS = ARTIFACTS / "results.json"

Check = tuple[str, bool, str]  # (label, passed, detail)


def check_expectations(saved: dict | None, expected: dict) -> list[Check]:
    saved = saved or {}
    checks: list[Check] = []
    if "year_founded" in expected:
        got = saved.get("year_founded") or 0
        checks.append(
            (
                f"year_founded≈{expected['year_founded']}",
                abs(int(got) - expected["year_founded"]) <= 1,
                f"got {got}",
            )
        )
    if "hq_contains" in expected:
        hq = saved.get("hq_location") or ""
        checks.append(
            (
                f"hq~{expected['hq_contains']!r}",
                expected["hq_contains"].lower() in hq.lower(),
                f"got {hq!r}",
            )
        )
    if "leader_contains" in expected:
        leaders = saved.get("leadership") or []
        sub = expected["leader_contains"].lower()
        checks.append(
            (
                f"leader~{expected['leader_contains']!r}",
                any(sub in str(x).lower() for x in leaders),
                f"got {leaders}",
            )
        )
    for fieldname in expected.get("non_empty", []):
        val = saved.get(fieldname)
        checks.append((f"{fieldname} non-empty", bool(val) and val != 0, ""))
    return checks


def trajectory_checks(tool_calls: list[str]) -> list[Check]:
    return [
        ("used web_search", "web_search" in tool_calls, ""),
        ("used fetch_page", "fetch_page" in tool_calls, ""),
        (
            "saved exactly once",
            tool_calls.count("save_company") == 1,
            f"{tool_calls.count('save_company')}×",
        ),
    ]


async def generate_traces(items: list[dict]) -> list[dict]:
    traces = []
    for i, item in enumerate(items, 1):
        print(f"[{i}/{len(items)}] enriching {item['name']}…")
        result = await enrich(item["name"], item["website"], item["topic"], verbose=False)
        traces.append(
            {
                "item": item,
                "saved": result.saved,
                "tool_calls": result.tool_calls,
                "summary": result.summary,
            }
        )
    return traces


async def grade(traces: list[dict]) -> list[dict]:
    graded = []
    for trace in traces:
        item, saved = trace["item"], trace["saved"]
        if saved:
            judgement = await judge_record(item["name"], saved)
        else:
            judgement = Judgement(
                accuracy=1, faithfulness=1, completeness=1, rationale="agent saved nothing"
            )
        field = check_expectations(saved, item["expected"])
        traj = trajectory_checks(trace["tool_calls"])
        passed = (
            all(c[1] for c in field) and judgement.accuracy >= 4 and judgement.faithfulness >= 4
        )
        graded.append(
            {
                "name": item["name"],
                "passed": passed,
                "field": field,
                "trajectory": traj,
                "judge": judgement.model_dump(),
            }
        )
    return graded


def report(graded: list[dict]) -> None:
    def fmt(checks):
        return "  ".join(("✓" if ok else "✗") + " " + label for label, ok, _ in checks)

    print("\n" + "=" * 70)
    for g in graded:
        j = g["judge"]
        print(f"\n{'PASS' if g['passed'] else 'FAIL'}  {g['name']}")
        print(
            f"   scores: accuracy={j['accuracy']} faithfulness={j['faithfulness']} completeness={j['completeness']}"
        )
        print(f"   fields: {fmt(g['field'])}")
        print(f"   trajct: {fmt(g['trajectory'])}")
        if j["likely_hallucinations"]:
            print(f"   ⚠ possible hallucinations: {j['likely_hallucinations']}")
        print(f"   judge:  {j['rationale']}")

    n = len(graded)
    if n:
        avg = lambda k: sum(g["judge"][k] for g in graded) / n  # noqa: E731
        field_rate = sum(all(c[1] for c in g["field"]) for g in graded) / n
        print("\n" + "-" * 70)
        print(f"OVERALL  {sum(g['passed'] for g in graded)}/{n} passed")
        print(
            f"  avg accuracy={avg('accuracy'):.1f}  faithfulness={avg('faithfulness'):.1f}  completeness={avg('completeness'):.1f}"
        )
        print(f"  field-check pass rate: {field_rate:.0%}")


async def _run(grade_only: bool, limit: int | None) -> None:
    ARTIFACTS.mkdir(exist_ok=True)
    try:
        if grade_only:
            traces = json.loads(TRACES.read_text())
        else:
            items = DATASET[:limit] if limit else DATASET
            traces = await generate_traces(items)
            TRACES.write_text(json.dumps(traces, indent=2))
        graded = await grade(traces)
        RESULTS.write_text(json.dumps(graded, indent=2))
        report(graded)
    finally:
        await close_driver()


def main() -> None:
    parser = argparse.ArgumentParser(description="Evaluate the enrichment agent.")
    parser.add_argument("--grade-only", action="store_true", help="Re-grade cached traces.json")
    parser.add_argument("--limit", type=int, help="Only the first N companies")
    args = parser.parse_args()
    asyncio.run(_run(args.grade_only, args.limit))


if __name__ == "__main__":
    main()
