import type { PersonPriorRole, PersonProposalDiffEntry } from "./types";

// Pure shaping of a person proposal's raw diff (`diff_person`'s `{field, old, new}`
// entries) into the review card's display groups (#178). Kept DB-free and side-effect
// free so it stays trivially testable once a frontend harness lands (#162) — the card
// only renders what this returns.

export interface PersonScalarChange {
  field: string;
  label: string;
  old: string | null; // null when newly sourced (no prior value)
  value: string;
  status: "new" | "changed";
}

export interface ShapedPersonDiff {
  updated: PersonScalarChange[]; // scalars replacing an existing value
  added: PersonScalarChange[]; // scalars newly sourced (no prior value)
  talks: string[]; // newly added talk URLs (still http(s)-guarded at render)
  priorRoles: PersonPriorRole[]; // proposed prior roles
  changeCount: number; // total committable changes — 0 means nothing to commit
}

// The scalar person facts, in a stable display order, with their human labels.
const SCALAR_LABELS: Record<string, string> = {
  title: "Title",
  bio: "Bio",
  linkedin: "LinkedIn",
  personal_site: "Personal site",
};
const SCALAR_ORDER = ["title", "bio", "linkedin", "personal_site"];

function asString(v: unknown): string {
  return v === null || v === undefined ? "" : String(v);
}

export function shapePersonDiff(
  diff: PersonProposalDiffEntry[] | undefined,
): ShapedPersonDiff {
  const updated: PersonScalarChange[] = [];
  const added: PersonScalarChange[] = [];
  let talks: string[] = [];
  let priorRoles: PersonPriorRole[] = [];

  for (const entry of diff ?? []) {
    if (entry.field in SCALAR_LABELS) {
      const old = asString(entry.old) || null;
      const change: PersonScalarChange = {
        field: entry.field,
        label: SCALAR_LABELS[entry.field],
        old,
        value: asString(entry.new),
        status: old ? "changed" : "new",
      };
      (old ? updated : added).push(change);
    } else if (entry.field === "talks") {
      const oldTalks = Array.isArray(entry.old) ? (entry.old as string[]) : [];
      const newTalks = Array.isArray(entry.new) ? (entry.new as string[]) : [];
      talks = newTalks.filter((t) => !oldTalks.includes(t));
    } else if (entry.field === "prior_roles") {
      priorRoles = Array.isArray(entry.new) ? (entry.new as PersonPriorRole[]) : [];
    }
  }

  const order = (c: PersonScalarChange) => SCALAR_ORDER.indexOf(c.field);
  updated.sort((a, b) => order(a) - order(b));
  added.sort((a, b) => order(a) - order(b));

  const changeCount =
    updated.length + added.length + talks.length + priorRoles.length;

  return { updated, added, talks, priorRoles, changeCount };
}
