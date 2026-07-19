// Pure shaping for the Market-thesis panel (#195, epic #192). Kept out of the
// component (like dates.ts / filters.ts) so the label/format logic is unit-tested
// without React. The thesis is a stored model of who acquires whom; these helpers
// turn a rule's controlled-vocabulary kinds, origin, and confidence into display
// text. No I/O, no DOM — deterministic.

import type { ThesisRule } from "./types";

// Controlled-vocabulary kind → human label. The vocab is small (cloud_provider,
// service_provider, isv, client…) but open-ended — the thesis is DATA and can grow
// without a deploy — so unknown kinds are humanised generically (snake→spaced,
// capitalised) rather than dropped. Acronym kinds stay upper-cased.
const KIND_ACRONYMS = new Set(["isv"]);

export function humanizeKind(kind: string): string {
  const token = (kind ?? "").trim();
  if (!token) return "—";
  if (KIND_ACRONYMS.has(token.toLowerCase())) return token.toUpperCase();
  const spaced = token.replace(/_/g, " ").trim();
  return spaced.charAt(0).toUpperCase() + spaced.slice(1);
}

// "Cloud provider → Service provider" — the rule's acquirer→target shape, a compact
// gloss beside the human-readable statement.
export function thesisPair(rule: Pick<ThesisRule, "acquirer_kind" | "target_kind">): string {
  return `${humanizeKind(rule.acquirer_kind)} → ${humanizeKind(rule.target_kind)}`;
}

// Who authored the rule. `origin` is a restricted vocab (user | reviewer) so crawled
// content can never masquerade as human-authored; unknown values pass through
// capitalised rather than being hidden.
export function originLabel(origin: string | null | undefined): string {
  if (origin === "user") return "Maintainer";
  if (origin === "reviewer") return "Reviewer";
  const token = (origin ?? "").trim();
  if (!token) return "—";
  return token.charAt(0).toUpperCase() + token.slice(1);
}

// Confidence (a probability in [0,1]) → a clamped whole-percent label, e.g. "75%".
export function confidenceLabel(confidence: number | null | undefined): string {
  const n = typeof confidence === "number" && Number.isFinite(confidence) ? confidence : 0;
  const clamped = Math.max(0, Math.min(1, n));
  return `${Math.round(clamped * 100)}%`;
}
