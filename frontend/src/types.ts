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

export const KINDS = ["service_provider", "isv", "cloud_provider", "client"] as const;

export function kindLabel(kind: string | null): string {
  if (kind === "service_provider") return "Service provider";
  if (kind === "isv") return "ISV";
  if (kind === "cloud_provider") return "Cloud provider";
  if (kind === "client") return "Client";
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
  discovered_website?: string; // set when a backlog stub's official site was found
  error?: string;
}

// --- Research backlog (ranked un-researched stubs, issue #30/#31) -------------

export interface BacklogRow {
  name: string;
  mention_count: number;
  client_mentions: number;
  partner_mentions: number;
  cloud_isv_partner_mentions: number;
  rank_score: number;
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

// --- Chat-proposed merge (issue #64) -----------------------------------------
// A user-named merge the assistant prepared: the named companies, which one
// survives, and (if the tool swapped the survivor to protect researched data) why.
// Committed via the existing resolution commit endpoint — the assistant never merges.

export interface MergeMember {
  name: string;
  edges: number;
  researched: boolean;
}

export interface MergeProposal {
  job_id: string;
  canonical: string;
  members: MergeMember[];
  canonical_reason?: string;
}

// --- Client-kind classification (bulk-label end-customer stubs) ---------------

export interface ClientCandidate {
  name: string;
  inbound: number; // count of inbound HAS_CLIENT edges
}

export interface Classification {
  job_id: string;
  status: "pending" | "ready" | "error";
  candidates: ClientCandidate[];
  stub_count: number;
  error?: string;
}

// Interactive graph view (issue #50). A node's 1-hop neighbourhood, fetched
// lazily per node so the client never renders the whole graph at once.
export interface GraphNode {
  id: string; // "<Label>:<name>", stable across expansions
  kind: string; // "Company" | "Person" | "Topic" | "CompanyType"
  name: string;
  companyKind: string | null;
  website: string | null;
  researched: boolean; // Company tagged to a topic (vs a partner/client stub)
}

export interface GraphEdge {
  source: string;
  target: string;
  type: string; // HAS_CLIENT | PARTNERS_WITH | LEADS | TAGGED_AS | CLASSIFIED_AS
}

export interface CompanyGraph {
  center: string;
  nodes: GraphNode[];
  edges: GraphEdge[];
}

// Similarity search (issue #32): another researched company that overlaps with a
// given one, with each scoring component returned so the "why" is explainable.
export interface SimilarCompany {
  name: string;
  score: number;
  shared_clients: number;
  shared_partners: number;
  shared_topics: number;
  same_kind: boolean;
  same_country: boolean;
}

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
