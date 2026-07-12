"""Web discovery: find companies matching a cohort profile that aren't yet in the
graph (issue #75).

The shipped in-graph `similar_companies` (issue #32) finds similarity *within* the
graph. This package turns that cohort into a TEMPLATE for an outward-facing web
search: derive a search profile from the seed + its similar cohort, generate a
handful of targeted queries, extract candidate companies from the results, drop
anything already captured (name / alias / domain), and surface the survivors — each
with a "why" and source links — for the user to review and feed into the existing
research pipeline. Nothing auto-writes; searched content only ever proposes.

The heuristics (profile facts, query generation, candidate extraction, dedup) are
pure so they test without a DB or a model; the durable job that stitches them
together lives in `discovery.py`.
"""
