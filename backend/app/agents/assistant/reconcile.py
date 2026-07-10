"""Heuristic people reconciliation for enrichment proposals.

When a re-research turns up leaders who are name-variants of people already in the
graph — "Andy Smith" when an "Andrew Smith" already exists — the additive MERGE in
`upsert_company` would create a *second* :Person node. This module matches proposed
leaders against existing ones (and against each other) using a nickname map + a
surname check, so:

- confident matches ("Andy"↔"Andrew", same surname) reconcile onto the existing
  canonical name and never write a duplicate;
- near-misses (an initial vs a full first name) are surfaced as "possibly the same
  as X" for the user to judge, and are NOT written automatically.

Deterministic and offline — no LLM call (see the proposal-diff flow in proposals.py).
"""

import re

# Short form → canonical full form. Both a nickname and its full name canonicalize
# to the same value, so "Andy" and "Andrew" collide. Covers the common English
# given names; unknown names are left as-is (so unusual pairs simply won't match —
# they surface as separate people rather than being wrongly merged).
_NICKNAMES = {
    "andy": "andrew",
    "drew": "andrew",
    "rob": "robert",
    "robbie": "robert",
    "bob": "robert",
    "bobby": "robert",
    "bill": "william",
    "billy": "william",
    "will": "william",
    "willy": "william",
    "dick": "richard",
    "rick": "richard",
    "rich": "richard",
    "richie": "richard",
    "jim": "james",
    "jimmy": "james",
    "jamie": "james",
    "mike": "michael",
    "mick": "michael",
    "micky": "michael",
    "tom": "thomas",
    "tommy": "thomas",
    "dave": "david",
    "davey": "david",
    "dan": "daniel",
    "danny": "daniel",
    "joe": "joseph",
    "joey": "joseph",
    "chris": "christopher",
    "topher": "christopher",
    "matt": "matthew",
    "matty": "matthew",
    "nick": "nicholas",
    "nicky": "nicholas",
    "tony": "anthony",
    "ed": "edward",
    "eddie": "edward",
    "ted": "edward",
    "teddy": "edward",
    "ben": "benjamin",
    "benji": "benjamin",
    "sam": "samuel",
    "alex": "alexander",
    "sandy": "alexander",
    "greg": "gregory",
    "jeff": "jeffrey",
    "geoff": "geoffrey",
    "ken": "kenneth",
    "kenny": "kenneth",
    "steve": "steven",
    "stevie": "steven",
    "pete": "peter",
    "phil": "philip",
    "ron": "ronald",
    "ronnie": "ronald",
    "don": "donald",
    "donnie": "donald",
    "fred": "frederick",
    "freddie": "frederick",
    "jack": "john",
    "johnny": "john",
    "jon": "jonathan",
    "larry": "lawrence",
    "marty": "martin",
    "gabe": "gabriel",
    "raj": "rajesh",
    # feminine
    "kate": "katherine",
    "katie": "katherine",
    "kathy": "katherine",
    "kat": "katherine",
    "cathy": "catherine",
    "cath": "catherine",
    "liz": "elizabeth",
    "lizzie": "elizabeth",
    "beth": "elizabeth",
    "betty": "elizabeth",
    "eliza": "elizabeth",
    "sue": "susan",
    "susie": "susan",
    "maggie": "margaret",
    "meg": "margaret",
    "peggy": "margaret",
    "jen": "jennifer",
    "jenny": "jennifer",
    "becky": "rebecca",
    "bex": "rebecca",
    "abby": "abigail",
    "gail": "abigail",
    "trish": "patricia",
    "patty": "patricia",
    "pat": "patricia",
    "debbie": "deborah",
    "deb": "deborah",
    "vicky": "victoria",
    "vic": "victoria",
    "mandy": "amanda",
    "steph": "stephanie",
    "nikki": "nicole",
    "angie": "angela",
    "val": "valerie",
    "cindy": "cynthia",
    "connie": "constance",
    "fran": "frances",
    "franny": "frances",
    "chrissy": "christine",
    "sandra": "sandra",
}


def _norm(text: str | None) -> str:
    """Casefold, strip surrounding punctuation, and collapse internal whitespace."""
    if not text:
        return ""
    cleaned = re.sub(r"[.’']", "", text)  # drop periods and apostrophes
    return re.sub(r"\s+", " ", cleaned).strip().casefold()


def _split_name(full: str) -> tuple[str, str]:
    """(first token, last token). Middle names are ignored; a single token has no
    surname. Trailing role fragments after a comma are dropped ("Smith, CEO")."""
    head = full.split(",")[0]
    tokens = [t for t in re.split(r"\s+", head.strip()) if t]
    if not tokens:
        return "", ""
    if len(tokens) == 1:
        return tokens[0], ""
    return tokens[0], tokens[-1]


def _canon_first(first: str) -> str:
    n = _norm(first)
    return _NICKNAMES.get(n, n)


def _is_initial(short: str, full: str) -> bool:
    """True if `short` is a bare initial matching the first letter of `full`
    (e.g. "A" or "A." for "Andrew")."""
    s = _norm(short)
    f = _norm(full)
    return len(s) == 1 and len(f) > 1 and s == f[:1]


def match_person(a: str, b: str) -> str:
    """Compare two full names: 'same' (confident), 'maybe' (needs review), or 'no'."""
    fa, la = _split_name(a)
    fb, lb = _split_name(b)
    na, nb = _norm(la), _norm(lb)
    if na and nb:
        if na != nb:
            return "no"  # different surnames → different people
        if _canon_first(fa) == _canon_first(fb):
            return "same"  # same surname + same (canonicalised) first name
        if _is_initial(fa, fb) or _is_initial(fb, fa):
            return "maybe"  # "A. Smith" vs "Andrew Smith"
        return "no"
    # One or both names have no surname — weaker signal, never confident.
    if _canon_first(fa) and _canon_first(fa) == _canon_first(fb):
        return "maybe"
    return "no"


def reconcile_people(existing: list[dict], proposed: list[dict]) -> dict:
    """Match proposed leaders against existing people (and against already-accepted
    proposed ones), so variants don't become duplicate nodes.

    Returns:
      reconciled: the leadership list to WRITE — canonical names, uncertain
                  variants excluded (so no probable duplicate is auto-created).
      added:      leaders genuinely new to the graph.
      merged:     confident variant merges, {proposed, canonical, title}.
      variants:   uncertain near-matches to surface, {name, title, possibly}.
    """
    reconciled: list[dict] = []
    added: list[dict] = []
    merged: list[dict] = []
    variants: list[dict] = []
    # Names we can match against: existing people, plus new proposed ones as we accept them.
    known: list[str] = [e["name"] for e in existing if e.get("name")]
    written: set[str] = set()  # normalized names already in `reconciled` (avoid dup rows)

    for person in proposed:
        name = (person.get("name") or "").strip()
        if not name:
            continue
        title = person.get("title")

        same_match = None
        maybe_match = None
        for candidate in known:
            verdict = match_person(name, candidate)
            if verdict == "same":
                same_match = candidate
                break
            if verdict == "maybe" and maybe_match is None:
                maybe_match = candidate

        if same_match is not None:
            if _norm(same_match) not in written:
                reconciled.append({"name": same_match, "title": title})
                written.add(_norm(same_match))
            elif title:  # same person already queued — keep a non-empty title
                for entry in reconciled:
                    if _norm(entry["name"]) == _norm(same_match):
                        entry["title"] = title
                        break
            if _norm(same_match) != _norm(name):
                merged.append({"proposed": name, "canonical": same_match, "title": title})
        elif maybe_match is not None:
            # Uncertain: flag it, but do NOT write (avoid creating a probable dup).
            variants.append({"name": name, "title": title, "possibly": maybe_match})
        else:
            reconciled.append({"name": name, "title": title})
            added.append({"name": name, "title": title})
            known.append(name)
            written.add(_norm(name))

    return {"reconciled": reconciled, "added": added, "merged": merged, "variants": variants}
