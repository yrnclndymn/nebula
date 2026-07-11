export interface CompanyRow {
  name: string;
  priority: string | null;
  about: string | null;
  website: string | null;
  linkedin: string | null;
  hqLocation: string | null;
  hqCountry: string | null;
  hqCity: string | null;
  hqState: string | null;
  headcount: number | null;
  estimatedRevenue: string | null;
  yearFounded: number | null;
  funding: string | null;
  notes: string | null;
  origin: string | null;
  kind: string | null;
  topics: string[];
  companyTypes: string[];
  partnerCount: number;
  clientCount: number;
  leaderCount: number;
  custom: Record<string, unknown>;
}

export const KINDS = ["service_provider", "isv", "cloud_provider"] as const;

export function kindLabel(kind: string | null): string {
  if (kind === "service_provider") return "Service provider";
  if (kind === "isv") return "ISV";
  if (kind === "cloud_provider") return "Cloud provider";
  return "—";
}

export interface FieldDef {
  name: string;
  label: string;
  description: string;
  appliesToKind: string; // a kind, or "all"
  type: string; // "list" | "text"
}

export function fieldApplies(fd: FieldDef, kind: string | null): boolean {
  return fd.appliesToKind === "all" || fd.appliesToKind === kind;
}

export function formatCustom(v: unknown): string {
  if (Array.isArray(v)) return v.length ? v.join(", ") : "—";
  if (v == null || v === "") return "—";
  return String(v);
}

export interface Leader {
  name: string;
  title: string | null;
}

export interface Citation {
  field: string;
  value: string;
  source: string;
  sourceDate: string | null;
}

// A proposed enrichment record (snake_case — it's the backend CompanyRecord).
export interface ProposalRecord {
  name: string;
  hq_location: string | null;
  year_founded: number | null;
  headcount: number | null;
  funding: string | null;
  estimated_revenue: string | null;
  about: string | null;
  origin: string | null;
  company_types: string[];
  partnerships: string[];
  clients: string[];
  leadership: { name: string; title: string | null }[];
  citations: { field: string; value: string; source: string; source_date: string | null }[];
}

export interface BackfillRow {
  company: string;
  value: unknown;
  source: string;
  committed: boolean;
}

export interface Backfill {
  job_id: string;
  status: "pending" | "ready";
  field: { name: string; label: string; type: string };
  total: number;
  done: number;
  rows: BackfillRow[];
}

export interface ScalarDiff {
  key: string;
  label: string;
  old: unknown;
  new: unknown;
  status: "new" | "changed" | "same";
}

export interface ListDiff {
  added: string[];
  existing_count: number;
}

export interface ProposalDiff {
  scalars: ScalarDiff[];
  clients: ListDiff;
  partners: ListDiff;
  leadership: {
    added: { name: string; title: string | null }[];
    merged: { proposed: string; canonical: string; title: string | null }[];
    variants: { name: string; title: string | null; possibly: string }[];
  };
}

export interface Proposal {
  proposal_id: string;
  name: string;
  status: "pending" | "ready" | "error";
  exists?: boolean;
  summary?: string;
  record?: ProposalRecord;
  focus_key?: string | null;
  focus_label?: string;
  diff?: ProposalDiff;
  error?: string;
}

// --- Entity resolution (stub dedup / alias / junk) ---------------------------

export interface ResolutionMember {
  name: string;
  edges: number;
}

export interface ResolutionCluster {
  canonical: string;
  members: ResolutionMember[];
  reason: "normalized" | "containment";
}

export interface Resolution {
  job_id: string;
  status: "pending" | "ready" | "error";
  clusters: ResolutionCluster[];
  junk: ResolutionMember[];
  stub_count: number;
  error?: string;
}

export type ResolutionDecision =
  | { action: "merge"; canonical: string; variants: string[] }
  | { action: "alias"; canonical: string; aliases: string[] }
  | { action: "junk"; names: string[] };

export interface CompanyDetail {
  name: string;
  priority: string | null;
  about: string | null;
  website: string | null;
  linkedin: string | null;
  hqLocation: string | null;
  hqCountry: string | null;
  hqCity: string | null;
  hqState: string | null;
  headcount: number | null;
  estimatedRevenue: string | null;
  yearFounded: number | null;
  funding: string | null;
  notes: string | null;
  origin: string | null;
  kind: string | null;
  topics: string[];
  companyTypes: string[];
  partners: string[];
  clients: string[];
  leadership: Leader[];
  citations: Citation[];
  custom: Record<string, unknown>;
}
