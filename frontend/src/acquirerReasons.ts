import type { AcquirerWhy } from "./types";
import { kindLabel } from "./types";

// Pure shaping for the #194 thesis-match acquirer reason. Renders the matched
// acquisition-thesis rule as "Service provider → ISV (n=6 supporting deals)": the
// candidate's kind → the target's kind, with a pluralized supporting-deal count. A
// freshly-seeded rule has no evidence yet, so a zero/absent count drops the
// parenthetical. Extracted (and unit-tested, #162) so the string logic stays pure and
// out of the JSX.
export function thesisMatchSummary(detail: AcquirerWhy["detail"]): string {
  const arrow = `${kindLabel(detail.acquirer_kind ?? null)} → ${kindLabel(detail.target_kind ?? null)}`;
  const n = detail.evidence ?? 0;
  const support = n > 0 ? ` (n=${n} supporting deal${n === 1 ? "" : "s"})` : "";
  return `${arrow}${support}`;
}
