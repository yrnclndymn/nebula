"""LLM-as-Judge, evidence-grounded (course Day 4).

The earlier judge scored against its own knowledge, which is stale and produced
false hallucination flags. This version judges *faithfulness to evidence*: given
the agent's citations and the text it actually retrieved, does each cited value
appear in / follow from that evidence? That's the right hallucination test and
mirrors what provenance is for in production — checking a number against its
source.
"""

import json

from google import genai
from google.genai import types
from pydantic import BaseModel, Field

from app.config import settings
from app.genai_retry import generate_with_retry

_PROMPT = """You are validating a research agent. You are given the record it \
saved (including a `citations` list of "field | value | source | date") and the \
EVIDENCE text it actually retrieved from the web.

Judge ONLY against the provided evidence — do NOT use outside knowledge.

For EACH citation, decide whether the evidence supports that value (supported = \
true/false) with a short note (quote/paraphrase the supporting text, or say "not \
in evidence"). Then score:
- faithfulness (1-5): are the cited values supported by the evidence? 5 = all \
supported; 1 = mostly unsupported or contradicted.
- completeness (1-5): did it cite the key checkable facts that appear in the \
evidence (HQ, founding year, funding, headcount)?

Company: {name}

Saved record:
{record}

Evidence:
{evidence}
"""


class ClaimCheck(BaseModel):
    field: str
    value: str
    supported: bool
    note: str = ""


class Judgement(BaseModel):
    faithfulness: int = Field(ge=1, le=5)
    completeness: int = Field(ge=1, le=5)
    claim_checks: list[ClaimCheck] = Field(default_factory=list)
    rationale: str = ""


async def judge_record(name: str, saved: dict, evidence: list[str]) -> Judgement:
    evidence_text = "\n\n".join(evidence)[:9000] or "(no evidence captured)"
    client = genai.Client()
    resp = await generate_with_retry(
        client,
        model=settings.gemini_model,
        contents=_PROMPT.format(
            name=name, record=json.dumps(saved, indent=2), evidence=evidence_text
        ),
        config=types.GenerateContentConfig(
            response_mime_type="application/json",
            response_schema=Judgement,
            temperature=0,
        ),
    )
    parsed = resp.parsed
    return parsed if isinstance(parsed, Judgement) else Judgement(faithfulness=1, completeness=1)
