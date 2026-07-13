"""Link people to captured signals (#41) — extraction + matching.

Signal capture is a content stream over UNTRUSTED crawled/searched text. This
module turns that text into people links without ever letting it steer a
company-fact write:

- **Extraction** (pure): detect blog author bylines, people quoted in articles,
  and event speakers from a signal's title + summary. Heuristics only — no LLM —
  so they are deterministic and unit-tested. A conservative name validator keeps
  obvious non-names (org suffixes, capitalised nouns, single tokens) out.
- **Matching** (pure): ``resolve_mention`` applies the precedence the story asks
  for — LinkedIn identity > exactly one same-name leader of the signal's company
  > *flag*. Anything short of a confident identity is flagged, never silently
  linked (the same conservatism as company-side reconciliation).
- **Orchestration**: ``link_signal_people`` wires extraction + the graph-side
  candidate lookup + the append-only edge writes in
  :mod:`app.graph.signals`. Confident matches attach to the existing person;
  everything else attaches to a clearly-marked flagged stub for later review.

The actual Cypher (candidate lookup + edge/stub writes) lives in
``app.graph.signals`` so this module holds only pure logic + thin wiring, and the
graph module never imports back into ``capture`` (no cycle).
"""

import logging
import re
from dataclasses import dataclass

from app.graph.models import SignalRecord
from app.graph.person_identity import canonical_linkedin

logger = logging.getLogger("nebula.capture.people")

# Relationship types a person can have to a signal. Fixed allowlist — the graph
# writer interpolates the type into Cypher (rel types can't be parameterised), so
# it must only ever come from this tuple.
AUTHORED = "AUTHORED"
QUOTED_IN = "QUOTED_IN"
SPOKE_AT = "SPOKE_AT"
SIGNAL_PERSON_RELATIONS = (AUTHORED, QUOTED_IN, SPOKE_AT)

# Priority when the same name is detected under several relations (an author who
# also quotes themselves): keep the strongest single relation.
_RELATION_PRIORITY = {AUTHORED: 0, SPOKE_AT: 1, QUOTED_IN: 2}


@dataclass(frozen=True)
class PersonMention:
    """One person detected in a signal, with the relation they hold to it."""

    name: str
    relation: str
    linkedin: str | None = None


@dataclass(frozen=True)
class ResolvedLink:
    """A mention resolved against the graph: either a confident link to an
    existing person (``target_eid`` set, ``flagged`` false) or a flagged stub
    (``target_eid`` None) that a human reviews before it is trusted."""

    name: str
    relation: str
    target_eid: str | None
    flagged: bool
    reason: str | None


# --- pure: name validation --------------------------------------------------

# A single capitalised name token: an initial ("Q.") or a capitalised word that
# ends in a lowercase letter ("Doe", "O'Neil", "Jean-Luc"). Ending on a lowercase
# letter keeps a trailing sentence period out of the token — so "said John Smith."
# yields "John Smith", and "Jane Doe. Jane Doe" stays two names, not one.
_NAME_TOKEN = r"(?:[A-Z]\.|[A-Z][A-Za-z'’\-]*[a-z])"
_NAME_RE = re.compile(rf"{_NAME_TOKEN}(?:\s+{_NAME_TOKEN}){{1,3}}")

# Tokens that mark a phrase as an organisation or a non-name noun rather than a
# person. Lowercased, trailing dot stripped before lookup. Deliberately broad —
# a false reject just means one fewer (best-effort) link, while a false accept
# would mint a junk person node.
_STOPWORDS = frozenset(
    {
        # org suffixes / forms
        "inc",
        "ltd",
        "llc",
        "llp",
        "corp",
        "corporation",
        "co",
        "company",
        "gmbh",
        "plc",
        "ag",
        "sa",
        "se",
        "nv",
        "oy",
        "ab",
        "group",
        "holdings",
        "technologies",
        "technology",
        "labs",
        "systems",
        "solutions",
        "ventures",
        "capital",
        "partners",
        "foundation",
        "institute",
        "university",
        "bank",
        # event / content nouns
        "summit",
        "conference",
        "webinar",
        "podcast",
        "series",
        "report",
        "news",
        "press",
        "release",
        "blog",
        "team",
        "keynote",
        "panel",
        # function words / initialisms that never stand as a name token
        "the",
        "and",
        "of",
        "for",
        "with",
        "ai",
        "api",
        "ceo",
        "cto",
        "coo",
        "cfo",
        "vp",
    }
)


def looks_like_person_name(text: str) -> bool:
    """Conservative check that ``text`` is a human name (2–4 capitalised tokens).

    Rejects single tokens (never confident evidence), anything with a digit, and
    any phrase carrying an org suffix / non-name noun (see ``_STOPWORDS``). Pure.
    """
    tokens = (text or "").split()
    if not (2 <= len(tokens) <= 4):
        return False
    for token in tokens:
        if not re.fullmatch(_NAME_TOKEN, token):
            return False
        if token.strip(".").lower() in _STOPWORDS:
            return False
    return True


# --- pure: extraction -------------------------------------------------------

# Where a candidate name segment ends after a "by X" / "Speakers: X" trigger.
_SEGMENT_END = re.compile(r"\s+(?:on|at|for|in|from|of|to|via)\s+|[.;:|·—–\n]")
_CONNECTORS = re.compile(r"\s*(?:,|&|\band\b)\s*")

_BYLINE_TRIGGER = re.compile(
    r"\b(?:written\s+by|posted\s+by|authored\s+by|author|by)\b[:\s]+", re.IGNORECASE
)
_SPEAKER_TRIGGER = re.compile(
    r"\b(?:speakers?|panel(?:ists?|lists?)?|presenters?|featuring|keynote\s+by|"
    r"presented\s+by|hosted\s+by)\b[:\s]+",
    re.IGNORECASE,
)

# Speech verbs signalling a quoted person. The verb group is case-insensitive
# (a sentence may open with "According to …") but the name group stays
# case-sensitive so only genuinely capitalised names are captured.
_SPEECH_VERBS = (
    r"said|says|told|adds|added|explains|explained|notes|noted|wrote|writes|"
    r"comments|commented|states|stated|argued|announced|recalled|continued"
)
_QUOTE_AFTER = re.compile(rf"\b(?i:said|says|according to|per)\s+({_NAME_RE.pattern})")
_QUOTE_BEFORE = re.compile(rf"({_NAME_RE.pattern})\s+(?i:{_SPEECH_VERBS})\b")


def _names_after_trigger(text: str, trigger: re.Pattern) -> list[str]:
    """Names following a segment trigger (``by`` / ``Speakers:`` / ``featuring``).

    For each trigger occurrence, take the text up to the next segment terminator,
    split it on connectors (``and`` / ``&`` / ``,``) and keep pieces that validate
    as person names. Order-preserving, de-duplicated.
    """
    out: list[str] = []
    for match in trigger.finditer(text or ""):
        tail = text[match.end() :]
        end = _SEGMENT_END.search(tail)
        segment = tail[: end.start()] if end else tail
        for piece in _CONNECTORS.split(segment):
            candidate = piece.strip()
            name_match = _NAME_RE.match(candidate)
            if name_match and looks_like_person_name(name_match.group()):
                name = name_match.group()
                if name not in out:
                    out.append(name)
    return out


def _names_from_verbs(text: str) -> list[str]:
    """Names adjacent to a speech verb (``X said`` / ``said X``). Order-preserving."""
    out: list[str] = []
    for pattern in (_QUOTE_BEFORE, _QUOTE_AFTER):
        for match in pattern.finditer(text or ""):
            name = match.group(1)
            if looks_like_person_name(name) and name not in out:
                out.append(name)
    return out


def extract_bylines(text: str) -> list[str]:
    """Author names from ``By X`` / ``Written by X`` / ``Author: X`` bylines."""
    return _names_after_trigger(text, _BYLINE_TRIGGER)


def extract_speakers(text: str) -> list[str]:
    """Speaker names from ``Speakers: …`` / ``featuring …`` / ``keynote by …``."""
    return _names_after_trigger(text, _SPEAKER_TRIGGER)


def extract_quoted(text: str) -> list[str]:
    """People quoted in the text (``X said`` / ``said X`` / ``according to X``)."""
    return _names_from_verbs(text)


def extract_people(kind: str, title: str, summary: str | None = None) -> list[PersonMention]:
    """People mentioned in a signal, by kind. Pure and deterministic.

    - ``blog``  → author bylines (AUTHORED) + anyone quoted (QUOTED_IN)
    - ``news``  → anyone quoted (QUOTED_IN)  [third-party bylines are journalists,
      not ecosystem people, so they are intentionally ignored]
    - ``event`` → speakers (SPOKE_AT)

    Names are de-duplicated across relations keeping the strongest (AUTHORED >
    SPOKE_AT > QUOTED_IN), so an author who quotes themselves yields one mention.
    """
    text = ". ".join(part for part in (title, summary) if part)
    collected: list[tuple[str, str]] = []
    if kind == "blog":
        collected += [(n, AUTHORED) for n in extract_bylines(text)]
        collected += [(n, QUOTED_IN) for n in extract_quoted(text)]
    elif kind == "news":
        collected += [(n, QUOTED_IN) for n in extract_quoted(text)]
    elif kind == "event":
        collected += [(n, SPOKE_AT) for n in extract_speakers(text)]
    else:
        return []

    best: dict[str, str] = {}
    for name, relation in collected:
        current = best.get(name)
        if current is None or _RELATION_PRIORITY[relation] < _RELATION_PRIORITY[current]:
            best[name] = relation
    return [PersonMention(name=n, relation=r) for n, r in best.items()]


# --- pure: matching precedence ----------------------------------------------


def resolve_mention(
    mention: PersonMention,
    *,
    linkedin_eids: list[str],
    name_company_eids: list[str],
) -> ResolvedLink:
    """Resolve a mention against graph candidates: LinkedIn > name@company > flag.

    Pure. ``linkedin_eids`` are Person nodes keyed on the mention's canonical
    LinkedIn URL (≤1 under the uniqueness constraint); ``name_company_eids`` are
    Persons with the exact name who lead the signal's company. Only a LinkedIn
    identity or a *single* same-name leader is a confident (unflagged) link;
    ambiguity (several leaders) or no evidence (unknown name) is flagged and
    routed to a review stub — never silently attached to a guessed person.
    """
    if mention.linkedin and linkedin_eids:
        return ResolvedLink(mention.name, mention.relation, linkedin_eids[0], False, "linkedin")
    if len(name_company_eids) == 1:
        return ResolvedLink(
            mention.name, mention.relation, name_company_eids[0], False, "name-at-company"
        )
    if len(name_company_eids) >= 2:
        return ResolvedLink(
            mention.name,
            mention.relation,
            None,
            True,
            f"ambiguous: {len(name_company_eids)} people named {mention.name!r} lead this company",
        )
    return ResolvedLink(
        mention.name,
        mention.relation,
        None,
        True,
        f"unverified: no existing person named {mention.name!r} at this company",
    )


# --- orchestration ----------------------------------------------------------


async def link_signal_people(driver, record: SignalRecord, company: str) -> dict:
    """Extract people from a captured signal and write their links to the graph.

    Best-effort and side-effect-bounded: it only ever creates ``:Person`` review
    stubs and ``AUTHORED|QUOTED_IN|SPOKE_AT`` edges — never touches company facts.
    Confident matches (LinkedIn / a lone same-name leader) attach to the existing
    person unflagged; every other mention attaches to a flagged stub. Returns
    ``{"linked": n, "flagged": n}``.
    """
    from app.graph import signals

    mentions = extract_people(record.kind, record.title, record.summary)
    if not mentions:
        return {"linked": 0, "flagged": 0}

    resolved: list[ResolvedLink] = []
    for mention in mentions:
        canon = canonical_linkedin(mention.linkedin)
        candidates = await signals.person_signal_candidates(
            driver, name=mention.name, company=company, linkedin_canon=canon
        )
        resolved.append(
            resolve_mention(
                mention,
                linkedin_eids=candidates["linkedin_eids"],
                name_company_eids=candidates["name_company_eids"],
            )
        )
    return await signals.write_person_signal_links(
        driver, record.canonical_url(), resolved, source=record.source
    )
