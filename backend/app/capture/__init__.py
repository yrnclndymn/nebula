"""Company-site signal capture (#34).

Given a researched company, capture recent items from its OWN site — news / blog /
press / events — via RSS/Atom feeds (autodiscovery + parsing) with an LLM-driven
index-page crawl as a fallback. Items are written as Signals through the existing
``app.graph.signals.upsert_signal`` write path, which dedupes on the canonical URL
(so re-runs only add new items) and records provenance.

Pure, test-first logic lives in ``feeds`` (discovery/parsing), ``dates`` (date
normalisation) and ``sections`` (index-page detection); ``job`` orchestrates the
durable, API-triggered capture job.
"""
