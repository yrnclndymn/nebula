"""One-off migration: canonicalise LinkedIn URLs already stored on Company nodes.

Rows added before the canonicalisation fix may hold a search-sourced variant such as
`https://uk.linkedin.com/company/x` (country subdomain, trailing slash). This rewrites
each to the canonical `https://www.linkedin.com/company/x`. Idempotent — safe to re-run.

    cd backend && uv run python scripts/normalize_linkedin.py [--dry-run]
"""

import argparse
import asyncio

from app.graph.driver import close_driver, get_driver
from app.tools.social import normalize_linkedin


async def _run(dry_run: bool) -> None:
    driver = get_driver()
    try:
        async with driver.session() as session:
            result = await session.run(
                "MATCH (c:Company) WHERE c.linkedin IS NOT NULL "
                "RETURN c.name AS name, c.linkedin AS linkedin ORDER BY name"
            )
            rows = [dict(record) async for record in result]

            changed = 0
            for row in rows:
                new = normalize_linkedin(row["linkedin"])
                if new == row["linkedin"]:
                    continue
                changed += 1
                print(f"  {row['name']}: {row['linkedin']} -> {new}")
                if not dry_run:
                    await session.run(
                        "MATCH (c:Company {name: $name}) SET c.linkedin = $new",
                        name=row["name"],
                        new=new,
                    )

        verb = "would update" if dry_run else "updated"
        print(f"\n{verb} {changed} of {len(rows)} company LinkedIn URL(s).")
    finally:
        await close_driver()


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--dry-run", action="store_true", help="show changes without writing")
    args = parser.parse_args()
    asyncio.run(_run(args.dry_run))


if __name__ == "__main__":
    main()
