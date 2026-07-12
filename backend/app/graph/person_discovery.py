"""Discover leaders' LinkedIn profiles and (on review) attach them — story #39.

Existing :Person nodes that lead a company but carry no LinkedIn URL are the
weak-identity case #39 fixes. This backfill discovers each leader's profile from
the company's OWN site (its team / leadership pages) and, as a fallback, a web
search — attaching a URL to a person ONLY when the profile slug deterministically
matches the person's name (`linkedin_slug_matches_name`). Crawled and searched
content is untrusted, so proximity on a page is never sufficient evidence.

Discovery is a **reviewable** step. By default it prints a dry-run report and
writes nothing: attaching a URL to an existing person mutates identity, so it
happens only when a human re-runs with ``--commit`` after reading the report.
Spend goes through the existing budget rails (`app.budget`) so a large graph
can't burn unbounded fetches/searches.

    cd backend && uv run python -m app.graph.person_discovery [--commit] [--limit N] [--company NAME]
    # or:  make discover-leader-linkedin ARGS="--commit --limit 10"
"""

import argparse
import asyncio
import re
from urllib.parse import urljoin, urlparse

import requests

from app import budget
from app.graph.driver import close_driver, get_driver
from app.graph.person_identity import (
    attach_linkedin,
    canonical_linkedin,
    extract_person_linkedins,
    linkedin_slug_matches_name,
)
from app.tools.encoding import response_text
from app.tools.web import web_search

_HEADERS = {"User-Agent": "Mozilla/5.0 (compatible; NebulaResearchBot/0.1)"}
_TEAM_HINTS = ("team", "about", "leadership", "people", "management", "founders", "who-we-are")
_MAX_PAGES_PER_COMPANY = 4


async def _leaders_missing_linkedin(driver, limit: int, company: str | None) -> list[dict]:
    company_clause = "AND c.name = $company" if company else ""
    async with driver.session() as session:
        result = await session.run(
            f"""
            MATCH (p:Person)-[:LEADS]->(c:Company)
            WHERE p.linkedin IS NULL AND c.website IS NOT NULL {company_clause}
            WITH c, collect(DISTINCT p.name) AS leaders
            RETURN c.name AS name, c.website AS website, leaders
            ORDER BY name LIMIT $limit
            """,
            company=company,
            limit=limit,
        )
        return [dict(record) async for record in result]


def _fetch(url: str) -> str:
    """Budget-charged raw fetch. Returns the page text, or '' on any error."""
    budget.charge_page()
    try:
        resp = requests.get(url, timeout=15, headers=_HEADERS)
        resp.raise_for_status()
        return response_text(resp)  # UTF-8-safe; no ISO-8859-1 mojibake (#89)
    except Exception:  # noqa: BLE001 — a dead/blocked page just yields no evidence
        return ""


def _candidate_pages(home_html: str, base_url: str) -> list[str]:
    """The home page plus a few internal links that look like team/leadership pages."""
    pages = [base_url]
    base_host = urlparse(base_url).netloc
    for href in re.findall(r'href=["\']([^"\'#\s]+)["\']', home_html, re.I):
        absolute = urljoin(base_url, href)
        if urlparse(absolute).netloc != base_host:
            continue
        if any(h in absolute.lower() for h in _TEAM_HINTS) and absolute not in pages:
            pages.append(absolute)
        if len(pages) >= _MAX_PAGES_PER_COMPANY:
            break
    return pages


def _match_from_urls(leader: str, urls: set[str]) -> str | None:
    for url in sorted(urls):
        if linkedin_slug_matches_name(url, leader):
            return url
    return None


def discover_for_company(name: str, website: str, leaders: list[str]) -> list[dict]:
    """Deterministically discover a LinkedIn profile for each leader of one company.

    Own-site evidence first (profiles linked from the company's own pages), then a
    per-leader web-search fallback — both gated on a slug↔name match.
    """
    home = _fetch(website)
    own_site_urls: set[str] = set()
    if home:
        for page in _candidate_pages(home, website):
            own_site_urls |= extract_person_linkedins(_fetch(page) if page != website else home)

    rows: list[dict] = []
    for leader in leaders:
        url = _match_from_urls(leader, own_site_urls)
        evidence = "own-site" if url else None
        if url is None:  # fallback: a targeted search, still slug-gated
            hits = web_search(f"{leader} {name} LinkedIn").get("results", [])
            search_urls = {c for h in hits if (c := canonical_linkedin(h.get("url")))}
            url = _match_from_urls(leader, search_urls)
            evidence = "search" if url else None
        if url:
            rows.append({"company": name, "person": leader, "url": url, "evidence": evidence})
    return rows


async def _run(commit: bool, limit: int, company: str | None) -> None:
    driver = get_driver()
    # Generous global caps sized to the batch; per-company fetching is bounded too.
    caps = budget.Budget(max_pages=limit * (_MAX_PAGES_PER_COMPANY + 2), max_searches=limit * 4)
    rows: list[dict] = []
    try:
        companies = await _leaders_missing_linkedin(driver, limit, company)
        with budget.use_budget(caps):
            for co in companies:
                try:
                    budget.charge_company()
                    rows.extend(
                        await asyncio.to_thread(
                            discover_for_company, co["name"], co["website"], co["leaders"]
                        )
                    )
                except budget.BudgetExhausted as exc:
                    print(f"! budget exhausted ({exc.limit}) — stopping discovery early")
                    break

        for row in rows:
            print(f"  {row['company']}: {row['person']} -> {row['url']}  [{row['evidence']}]")

        if not rows:
            print("no leader LinkedIn profiles discovered.")
        elif not commit:
            print(
                f"\n{len(rows)} profile(s) discovered. Re-run with --commit to attach (review above first)."
            )
        else:
            attached = 0
            for row in rows:
                result = await attach_linkedin(
                    driver, row["person"], row["url"], company=row["company"], dry_run=False
                )
                print(
                    f"  {result['action']}: {row['person']} [{result.get('canonical', row['url'])}]"
                )
                attached += result["action"] in ("set", "merged")
            print(f"\nattached {attached} of {len(rows)} discovered profile(s).")
        print(f"budget used: {caps.usage()}")
    finally:
        await close_driver()


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--commit", action="store_true", help="attach discovered URLs (default: dry-run report)"
    )
    parser.add_argument("--limit", type=int, default=25, help="max companies to scan (default 25)")
    parser.add_argument("--company", default="", help="restrict to one company by exact name")
    args = parser.parse_args()
    asyncio.run(_run(args.commit, args.limit, args.company or None))


if __name__ == "__main__":
    main()
