"""Tidy the free-text HQ into structured country / city / state.

This is normalization of EXISTING data (parsing hqLocation), not web research —
so it's cheap: a few batched LLM calls, no crawling. Runs in the background and
auto-applies, normalizing the country so scoping ("all UK companies") is reliable.
"""

import asyncio
import json

from google import genai
from google.genai import types
from pydantic import BaseModel

from app.config import settings
from app.genai_retry import generate_with_retry
from app.graph import queries
from app.graph.driver import get_driver

# Map common country variants to a canonical full name so filters group correctly.
_COUNTRY_ALIASES = {
    "uk": "United Kingdom",
    "u.k.": "United Kingdom",
    "gb": "United Kingdom",
    "great britain": "United Kingdom",
    "england": "United Kingdom",
    "scotland": "United Kingdom",
    "wales": "United Kingdom",
    "us": "United States",
    "usa": "United States",
    "u.s.": "United States",
    "u.s.a.": "United States",
    "united states of america": "United States",
    "america": "United States",
    "uae": "United Arab Emirates",
}


def normalize_country(country: str | None) -> str | None:
    if not country or not country.strip():
        return None
    value = country.strip()
    return _COUNTRY_ALIASES.get(value.lower(), value)


class _HQ(BaseModel):
    name: str
    country: str = ""
    city: str = ""
    state: str = ""


class _HQBatch(BaseModel):
    items: list[_HQ]


_PROMPT = """Parse each company's free-text HQ location into structured fields. For
each item return the name EXACTLY as given, plus country, city, and state (US
state / province if present, else ""). Use the full English country name (e.g.
"United Kingdom", "United States", "Germany"). Use "" for anything not present; if
the HQ is "Remote" or unknown, leave city and country "".

Items (JSON):
{items}"""


async def _parse_batch(client: genai.Client, batch: list[dict]) -> list[_HQ]:
    resp = await generate_with_retry(
        client,
        model=settings.gemini_model,
        contents=_PROMPT.format(items=json.dumps(batch)),
        config=types.GenerateContentConfig(
            response_mime_type="application/json",
            response_schema=_HQBatch,
            temperature=0,
        ),
    )
    parsed = resp.parsed
    return parsed.items if isinstance(parsed, _HQBatch) else []


async def _run_tidy() -> None:
    driver = get_driver()
    companies = await queries.companies_with_hq(driver)
    client = genai.Client()
    for start in range(0, len(companies), 20):
        batch = [{"name": c["name"], "hq": c["hq"]} for c in companies[start : start + 20]]
        try:
            items = await _parse_batch(client, batch)
        except Exception:  # noqa: BLE001 — skip a bad batch, keep going
            continue
        for item in items:
            await queries.set_hq(
                driver,
                item.name,
                normalize_country(item.country),
                item.city or None,
                item.state or None,
            )


async def tidy_hq() -> dict:
    """Tidy the HQ field for all companies: parse the free-text HQ into structured
    country / city / state and normalize the country. Runs in the background and
    applies automatically (no review). Use when the user asks to tidy or clean up
    the HQ field. Tell the user it's running and the Country/City will fill in
    shortly."""
    companies = await queries.companies_with_hq(get_driver())
    asyncio.create_task(_run_tidy())
    return {"tidying": len(companies), "status": "running in the background"}
