"""Name <-> domain host choice for website discovery (issue #67).

Observed prod false positive: for a well-known lab, website discovery took the
*first* non-blocklisted search hit, which was a *foundation* on a different
domain, not the lab's own site. "First organic result" is a weak signal — the
result that best *resembles the company name* is the better bet.

This module is the pure host-choice / similarity logic: given a company name and
a list of search results, rank the candidate hosts by how closely the host's
registrable label matches the normalised company name, and pick the best. Ties
fall back to the original search order, so when NOTHING resembles the name the
pick degrades gracefully to "first non-blocklisted result" (the legacy
behaviour) — no magic threshold required.

Everything here is pure (str/list in, str/float out) so it tests without a DB,
model, or network. The optional landing-page check is exposed as
`page_mentions_name` (pure over already-fetched text); the caller does the fetch.

The result hosts are UNTRUSTED web finds — the pick only ever seeds an
enrichment crawl the user reviews before any commit; it never steers a write.
"""

from dataclasses import dataclass

from app.agents.discovery.extract import official_domain
from app.graph.entity_resolution import normalized_tokens

# Two-level public suffixes we must peel to reach the registrable label
# ("acme.co.uk" -> "acme", not "co"). Deliberately a small, common set — an
# unknown two-level suffix just leaves an extra label, which only softens the
# similarity score, never corrupts it.
_TWO_LEVEL_SUFFIXES = frozenset(
    {
        "co.uk",
        "org.uk",
        "ac.uk",
        "gov.uk",
        "me.uk",
        "ltd.uk",
        "plc.uk",
        "com.au",
        "net.au",
        "org.au",
        "co.nz",
        "org.nz",
        "co.jp",
        "or.jp",
        "co.kr",
        "co.in",
        "co.za",
        "com.br",
        "com.cn",
        "com.sg",
        "com.hk",
        "com.mx",
        "com.tr",
        "com.tw",
    }
)


def domain_label(host: str) -> str:
    """The registrable label of a host: `www` and the public suffix stripped, and
    for a subdomain the registrable name (not the leaf).

    "www.acme.com" -> "acme"; "acme.co.uk" -> "acme";
    "foundation.acme.org" -> "acme" (the registrable label, which is what a
    company's name should resemble).
    """
    host = (host or "").lower().strip()
    # Tolerate a full URL as well as a bare host: drop scheme, path, userinfo, port.
    host = host.split("://", 1)[-1].split("/", 1)[0]
    host = host.split("@")[-1].split(":")[0].removeprefix("www.")
    if not host:
        return ""
    labels = host.split(".")
    if len(labels) >= 3 and ".".join(labels[-2:]) in _TWO_LEVEL_SUFFIXES:
        labels = labels[:-2]
    elif len(labels) >= 2:
        labels = labels[:-1]
    return labels[-1] if labels else ""


def name_domain_similarity(name: str, host: str) -> float:
    """How closely a host resembles a company name, in [0, 1].

    Compares the normalised, de-punctuated company name (legal suffixes stripped,
    tokens squished — "Nimbus Lab" -> "nimbuslab") against the host's registrable
    label. Exact match scores 1.0; a label that fully contains (or is contained
    by) the whole name key scores high (a company's site is often "<name>.com" or
    "<name>group.io"); otherwise the fraction of name tokens that appear in the
    label. An unrelated host scores 0.

    Deliberately substring/coverage-based, not a fuzzy edit ratio: over short
    domain labels an edit ratio gives unrelated hosts noisy, unequal scores, which
    would defeat the "ties fall back to search order" degrade. Here two hosts that
    genuinely don't resemble the name both score 0 and keep their search order.
    """
    tokens = normalized_tokens(name)
    if not tokens:
        return 0.0
    label = domain_label(host)
    if not label:
        return 0.0
    key = "".join(tokens)
    if label == key:
        return 1.0
    if key in label or label in key:
        # One fully contains the other (e.g. "nimbuslab" within "nimbuslabgroup")
        # — strong, but below an exact match.
        return 0.9
    # Otherwise: how many of the name's tokens appear in the label at all.
    covered = sum(1 for t in tokens if t in label)
    return (covered / len(tokens)) * 0.8


@dataclass
class RankedHost:
    """A candidate host with its name-resemblance score and originating result URL."""

    host: str
    score: float
    url: str


def rank_hosts(name: str, results: list[dict]) -> list[RankedHost]:
    """Rank the official-looking hosts in `results` by name resemblance, best first.

    Blocklisted (social/directory/press) hosts and non-http(s) URLs are dropped
    (reusing `extract.official_domain`), and each host appears once (first URL
    wins). The sort is stable on score, so equal-scoring hosts keep their original
    search order — which means an all-zero (nothing resembles the name) ranking
    returns the first non-blocklisted result first.
    """
    ranked: list[RankedHost] = []
    seen: set[str] = set()
    for hit in results:
        url = hit.get("url") or ""
        if not url.lower().startswith(("http://", "https://")):
            continue
        host = official_domain(url)
        if not host or host in seen:
            continue
        seen.add(host)
        ranked.append(RankedHost(host, name_domain_similarity(name, host), url))
    ranked.sort(key=lambda r: r.score, reverse=True)  # stable: ties keep search order
    return ranked


def best_host(name: str, results: list[dict]) -> str | None:
    """The single best host for `name` among `results`, or None if none qualify.

    Prefers the host that most resembles the name over raw search order; on ties
    (including the all-unresembling case) falls back to the earliest search hit.
    """
    ranked = rank_hosts(name, results)
    return ranked[0].host if ranked else None


def page_mentions_name(name: str, text: str) -> bool:
    """Does `text` (an already-fetched landing page) actually name the company?

    True iff every normalised name token appears as a whole word in the text
    (case-insensitive). A soft, optional confirmation the caller can use to
    disambiguate a weak host pick — a token buried inside a larger word
    ("lab" in "elaborate") does not count.
    """
    tokens = normalized_tokens(name)
    if not tokens:
        return False
    words = set(normalized_tokens(text))
    return all(t in words for t in tokens)
