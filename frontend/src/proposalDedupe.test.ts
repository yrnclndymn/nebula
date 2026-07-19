import { describe, it, expect } from "vitest";
import type { JobSummary } from "./types";
import { proposalSupersedes, dedupeProposalsByScope } from "./proposalDedupe";

// Characterisation tests (#162) for the scope-aware, newest-wins proposal dedupe (#102).

// Minimal JobSummary builder — only the fields the dedupe reads matter.
function job(
  id: string,
  name: string | undefined,
  focusKey: string | null,
  extra: Partial<JobSummary["summary"]> = {},
): JobSummary {
  return {
    id,
    type: "enrich_company",
    status: "done",
    createdAt: "2026-01-01T00:00:00Z",
    summary: { name, focus_key: focusKey, ...extra },
  };
}

describe("proposalSupersedes", () => {
  it("a full (null focus) newer attempt supersedes anything older", () => {
    expect(proposalSupersedes(null, null)).toBe(true);
    expect(proposalSupersedes(null, "headcount")).toBe(true);
  });

  it("a focused newer attempt supersedes only an older attempt at the same field", () => {
    expect(proposalSupersedes("headcount", "headcount")).toBe(true);
    expect(proposalSupersedes("headcount", "funding")).toBe(false);
  });

  it("a focused newer attempt never supersedes a full (null) older attempt", () => {
    expect(proposalSupersedes("headcount", null)).toBe(false);
  });
});

describe("dedupeProposalsByScope", () => {
  it("returns an empty array unchanged", () => {
    expect(dedupeProposalsByScope([])).toEqual([]);
  });

  it("keeps a newer full run and drops an older full run for the same name", () => {
    const jobs = [job("j2", "Acme", null), job("j1", "Acme", null)];
    expect(dedupeProposalsByScope(jobs).map((j) => j.id)).toEqual(["j2"]);
  });

  it("keeps jobs for different names independently", () => {
    const jobs = [job("j2", "Acme", null), job("j1", "Globex", null)];
    expect(dedupeProposalsByScope(jobs).map((j) => j.id)).toEqual(["j2", "j1"]);
  });

  it("keeps jobs without a name (can't be deduped)", () => {
    const jobs = [job("j2", undefined, null), job("j1", undefined, null)];
    expect(dedupeProposalsByScope(jobs).map((j) => j.id)).toEqual(["j2", "j1"]);
  });

  it("a newer full run supersedes an older focused run for the same name", () => {
    const jobs = [job("j2", "Acme", null), job("j1", "Acme", "headcount")];
    expect(dedupeProposalsByScope(jobs).map((j) => j.id)).toEqual(["j2"]);
  });

  it("a newer focused run does NOT supersede an older full run (both kept)", () => {
    const jobs = [job("j2", "Acme", "headcount"), job("j1", "Acme", null)];
    expect(dedupeProposalsByScope(jobs).map((j) => j.id)).toEqual(["j2", "j1"]);
  });

  it("a newer focused run supersedes an older focused run at the same field", () => {
    const jobs = [job("j2", "Acme", "headcount"), job("j1", "Acme", "headcount")];
    expect(dedupeProposalsByScope(jobs).map((j) => j.id)).toEqual(["j2"]);
  });

  it("keeps focused runs at different fields for the same name", () => {
    const jobs = [job("j2", "Acme", "headcount"), job("j1", "Acme", "funding")];
    expect(dedupeProposalsByScope(jobs).map((j) => j.id)).toEqual(["j2", "j1"]);
  });

  it("treats a missing focus_key as a full run (null)", () => {
    const jobs = [
      { id: "j2", type: "t", status: "done", createdAt: "x", summary: { name: "Acme" } },
      { id: "j1", type: "t", status: "done", createdAt: "x", summary: { name: "Acme" } },
    ] as JobSummary[];
    expect(dedupeProposalsByScope(jobs).map((j) => j.id)).toEqual(["j2"]);
  });
});
