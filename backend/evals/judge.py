"""LLM-as-Judge: score an agent's saved record on accuracy / faithfulness /
completeness (course Day 4). A separate model call assesses quality that
deterministic checks can't — is the data actually correct and un-hallucinated?
"""

import json

from google import genai
from google.genai import types
from pydantic import BaseModel, Field

from app.config import settings
from app.genai_retry import generate_with_retry

_PROMPT = """You are grading a research agent that gathered facts about a real \
company and saved them. Using your own knowledge of this company, score the saved \
record on a 1-5 scale (5 = best):

- accuracy: are the saved facts correct for this company?
- faithfulness: does it avoid fabricated or hallucinated specifics (wrong numbers, \
invented people/partners)?
- completeness: did it capture the key findable facts (what they do, HQ, founding \
year, leadership)?

List any fields that look hallucinated or wrong. Give a one-sentence rationale.

Company: {name}
Saved record (JSON):
{record}
"""


class Judgement(BaseModel):
    accuracy: int = Field(ge=1, le=5)
    faithfulness: int = Field(ge=1, le=5)
    completeness: int = Field(ge=1, le=5)
    likely_hallucinations: list[str] = Field(default_factory=list)
    rationale: str = ""


async def judge_record(name: str, saved: dict) -> Judgement:
    client = genai.Client()
    resp = await generate_with_retry(
        client,
        model=settings.gemini_model,
        contents=_PROMPT.format(name=name, record=json.dumps(saved, indent=2)),
        config=types.GenerateContentConfig(
            response_mime_type="application/json",
            response_schema=Judgement,
            temperature=0,
        ),
    )
    parsed = resp.parsed
    return (
        parsed
        if isinstance(parsed, Judgement)
        else Judgement(accuracy=1, faithfulness=1, completeness=1)
    )
