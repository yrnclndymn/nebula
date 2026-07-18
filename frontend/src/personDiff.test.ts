import { describe, it, expect } from "vitest";
import type { PersonProposalDiffEntry, PersonPriorRole } from "./types";
import { shapePersonDiff } from "./personDiff";

// Characterisation tests (#162) for shapePersonDiff (#178) — pure shaping of a
// person proposal's raw diff into the review card's display groups.

describe("shapePersonDiff", () => {
  it("returns empty groups for undefined or empty input", () => {
    for (const diff of [undefined, [] as PersonProposalDiffEntry[]]) {
      const shaped = shapePersonDiff(diff);
      expect(shaped).toEqual({
        updated: [],
        added: [],
        talks: [],
        priorRoles: [],
        changeCount: 0,
      });
    }
  });

  it("classes a scalar with a prior value as 'changed' into updated", () => {
    const shaped = shapePersonDiff([{ field: "title", old: "VP Eng", new: "CTO" }]);
    expect(shaped.updated).toEqual([
      { field: "title", label: "Title", old: "VP Eng", value: "CTO", status: "changed" },
    ]);
    expect(shaped.added).toEqual([]);
    expect(shaped.changeCount).toBe(1);
  });

  it("classes a scalar with no prior value as 'new' into added", () => {
    const shaped = shapePersonDiff([{ field: "bio", old: null, new: "A bio." }]);
    expect(shaped.added).toEqual([
      { field: "bio", label: "Bio", old: null, value: "A bio.", status: "new" },
    ]);
    expect(shaped.updated).toEqual([]);
    expect(shaped.changeCount).toBe(1);
  });

  it("treats an empty-string old value as no prior value (added, new)", () => {
    const shaped = shapePersonDiff([{ field: "linkedin", old: "", new: "https://x" }]);
    expect(shaped.added.map((c) => c.status)).toEqual(["new"]);
    expect(shaped.added[0].old).toBe(null);
  });

  it("stringifies non-string scalar values", () => {
    const shaped = shapePersonDiff([{ field: "title", old: 1, new: 2 }]);
    expect(shaped.updated[0]).toMatchObject({ old: "1", value: "2", status: "changed" });
  });

  it("sorts scalars into the stable label order across both groups", () => {
    // personal_site (new), title (changed) -> title sorts before personal_site
    const updated = shapePersonDiff([
      { field: "personal_site", old: null, new: "https://site" },
      { field: "title", old: "Old", new: "New" },
    ]);
    // Only 'title' has a prior value so lands in updated; personal_site in added.
    expect(updated.updated.map((c) => c.field)).toEqual(["title"]);
    expect(updated.added.map((c) => c.field)).toEqual(["personal_site"]);

    // Two added scalars out of order -> sorted by SCALAR_ORDER.
    const twoAdded = shapePersonDiff([
      { field: "personal_site", old: null, new: "https://site" },
      { field: "linkedin", old: null, new: "https://li" },
    ]);
    expect(twoAdded.added.map((c) => c.field)).toEqual(["linkedin", "personal_site"]);
  });

  it("keeps only talk URLs not already present (set difference)", () => {
    const shaped = shapePersonDiff([
      { field: "talks", old: ["https://a", "https://b"], new: ["https://b", "https://c"] },
    ]);
    expect(shaped.talks).toEqual(["https://c"]);
    expect(shaped.changeCount).toBe(1);
  });

  it("treats a non-array talks old value as empty", () => {
    const shaped = shapePersonDiff([{ field: "talks", old: null, new: ["https://a"] }]);
    expect(shaped.talks).toEqual(["https://a"]);
  });

  it("passes prior_roles through from the new value", () => {
    const roles: PersonPriorRole[] = [
      { company: "Globex", title: "Eng", from_year: 2018, to_year: 2020, source: "https://s" },
    ];
    const shaped = shapePersonDiff([{ field: "prior_roles", old: 2, new: roles }]);
    expect(shaped.priorRoles).toEqual(roles);
    expect(shaped.changeCount).toBe(1);
  });

  it("ignores unknown fields", () => {
    const shaped = shapePersonDiff([{ field: "mystery", old: "a", new: "b" }]);
    expect(shaped.changeCount).toBe(0);
  });

  it("sums changeCount across all groups", () => {
    const shaped = shapePersonDiff([
      { field: "title", old: "Old", new: "New" }, // updated
      { field: "bio", old: null, new: "Bio" }, // added
      { field: "talks", old: [], new: ["https://a", "https://b"] }, // 2 talks
      { field: "prior_roles", old: 0, new: [{ company: "Acme", title: null, from_year: null, to_year: null, source: null }] },
    ]);
    expect(shaped.changeCount).toBe(1 + 1 + 2 + 1);
  });
});
