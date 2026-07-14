import type { JobSummary } from "./types";

// Scope-aware, per-name newest-wins dedupe for rehydrated proposal jobs (#102).
// Pure (no React/DOM) so the scope rule is unit-testable DB-free and reusable.

// Does a newer proposal supersede an OLDER one for the SAME company name? Mirrors
// the backend rule: a full attempt (null focus) supersedes any older attempt; a
// focused attempt supersedes only an older attempt at the SAME field, and never a
// full enrichment. `focus` is the resolved focused field, or null for a full run.
export function proposalSupersedes(newerFocus: string | null, olderFocus: string | null): boolean {
  return newerFocus === null || newerFocus === olderFocus;
}

// `jobs` is newest-first: keep a job only when no already-kept (newer) job for the
// same name supersedes its scope. Run this BEFORE filtering out committed jobs —
// filtering committed first drops a fresh success and lets a stale error survive
// to drive the status badge (the reported bug).
export function dedupeProposalsByScope(jobs: JobSummary[]): JobSummary[] {
  const kept: JobSummary[] = [];
  for (const job of jobs) {
    const name = job.summary.name;
    if (!name) {
      kept.push(job);
      continue;
    }
    const focus = job.summary.focus_key ?? null;
    const superseded = kept.some(
      (newer) =>
        newer.summary.name === name &&
        proposalSupersedes(newer.summary.focus_key ?? null, focus),
    );
    if (!superseded) kept.push(job);
  }
  return kept;
}
