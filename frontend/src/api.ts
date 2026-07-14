import { getIdToken } from "./firebase";
import type {
  Backfill,
  BacklogRow,
  Classification,
  CompanyDetail,
  CompanyRow,
  Discovery,
  JobSummary,
  MergeProposal,
  Proposal,
  Resolution,
  ResolutionDecision,
  Signal,
  SignalCapture,
} from "./types";

// Backend base URL. Override in production via VITE_API_BASE (e.g. "/api").
export const API_BASE = import.meta.env.VITE_API_BASE ?? "http://localhost:8080";

async function request<T>(method: string, path: string, body?: unknown): Promise<T> {
  const headers: Record<string, string> = {};
  const token = await getIdToken();
  if (token) headers.Authorization = `Bearer ${token}`;
  if (body !== undefined) headers["Content-Type"] = "application/json";
  const resp = await fetch(`${API_BASE}${path}`, {
    method,
    headers,
    body: body !== undefined ? JSON.stringify(body) : undefined,
  });
  // A signed-in but non-allow-listed account: let the app show an access screen.
  if (resp.status === 403) window.dispatchEvent(new CustomEvent("nebula:forbidden"));
  if (!resp.ok) throw new Error(`${path} → ${resp.status}`);
  return resp.json() as Promise<T>;
}

const getJson = <T>(path: string) => request<T>("GET", path);
const postJson = <T>(path: string, body: unknown) => request<T>("POST", path, body);

export const sendChat = (sessionId: string, message: string) =>
  postJson<{
    reply: string;
    proposals: Proposal[];
    backfills: { job_id: string; field: string; total: number }[];
    merges: MergeProposal[];
  }>("/chat", { session_id: sessionId, message });

export const getBackfill = (jobId: string) => getJson<Backfill>(`/backfill/${jobId}`);

export const commitBackfill = (jobId: string, companies: string[] | null) =>
  postJson<{ committed?: number; error?: string }>(`/backfill/${jobId}/commit`, { companies });

export const setKind = (name: string, kind: string | null) =>
  request<{ name: string; kind: string | null }>(
    "PATCH",
    `/companies/${encodeURIComponent(name)}/kind`,
    { kind },
  );

export const getProposal = (proposalId: string) =>
  getJson<Proposal>(`/proposals/${encodeURIComponent(proposalId)}`);

export const commitProposal = (proposalId: string, scope: "focus" | "all" = "all") =>
  postJson<{ committed?: string; scope?: string; error?: string }>("/proposals/commit", {
    proposal_id: proposalId,
    scope,
  });

export const scanResolution = () =>
  postJson<{ job_id: string; status: string }>("/resolution/scan", {});

export const getResolution = (jobId: string) => getJson<Resolution>(`/resolution/${jobId}`);

export const commitResolution = (jobId: string, decisions: ResolutionDecision[]) =>
  postJson<{ merged?: number; aliased?: number; flagged?: number; error?: string }>(
    `/resolution/${jobId}/commit`,
    { decisions },
  );

export const scanClassification = () =>
  postJson<{ job_id: string; status: string }>("/classification/scan", {});

export const getClassification = (jobId: string) =>
  getJson<Classification>(`/classification/${jobId}`);

export const commitClassification = (jobId: string, names: string[]) =>
  postJson<{ classified?: number; error?: string }>(`/classification/${jobId}/commit`, { names });

export const fetchBacklog = (limit = 200) => getJson<BacklogRow[]>(`/backlog?limit=${limit}`);

// Recent durable jobs, newest first (issue #66) — rehydrates in-progress
// research after a refresh. Full dataJson stays on the per-id detail endpoints.
export const dismissJob = (jobId: string) =>
  request<{ dismissed: string }>("DELETE", `/jobs/${encodeURIComponent(jobId)}`);

export const listJobs = (params: { type?: string; status?: string; limit?: number } = {}) => {
  const qs = new URLSearchParams();
  if (params.type) qs.set("type", params.type);
  if (params.status) qs.set("status", params.status);
  if (params.limit) qs.set("limit", String(params.limit));
  const q = qs.toString();
  return getJson<JobSummary[]>(`/jobs${q ? `?${q}` : ""}`);
};

export const researchBacklog = (names: string[]) =>
  postJson<{ proposals: { name: string; proposal_id: string }[]; cap: number }>(
    "/backlog/research",
    { names },
  );

export const fetchCompanies = () => getJson<CompanyRow[]>("/companies");
export const fetchCompany = (name: string) =>
  getJson<CompanyDetail>(`/companies/${encodeURIComponent(name)}`);
export const fetchCompanyGraph = (name: string) =>
  getJson<import("./types").CompanyGraph>(`/companies/${encodeURIComponent(name)}/graph`);
export const fetchSimilar = (name: string) =>
  getJson<import("./types").SimilarCompany[]>(`/companies/${encodeURIComponent(name)}/similar`);

// Web discovery (issue #75): search the web for companies like a seed that aren't
// in the graph yet. Start returns a durable job id (or a note if there's no
// cohort to search from); poll it, then feed selected candidates to research.
export const startDiscovery = (name: string) =>
  postJson<{ job_id?: string; seed: string; cohort?: number; candidates?: number; note?: string }>(
    `/companies/${encodeURIComponent(name)}/discover`,
    {},
  );

export const getDiscovery = (jobId: string) => getJson<Discovery>(`/discovery/${jobId}`);

export const researchDiscovery = (jobId: string, names: string[]) =>
  postJson<{ proposals: { name: string; proposal_id: string }[]; cap: number }>(
    `/discovery/${jobId}/research`,
    { names },
  );
export const fetchTopics = () => getJson<string[]>("/topics");
export const fetchCompanyTypes = () => getJson<string[]>("/company-types");
export const fetchFields = () => getJson<import("./types").FieldDef[]>("/fields");
export const fetchCountries = () => getJson<string[]>("/countries");

// Own-site signal capture (issue #34): start returns a durable job id; poll it
// for captured/new counts. The job also appears on the activity page.
export const startSignalCapture = (name: string) =>
  postJson<{ job_id: string; status: string }>(
    `/companies/${encodeURIComponent(name)}/signals/capture`,
    {},
  );

export const getSignalCapture = (jobId: string) =>
  getJson<SignalCapture>(`/signals/capture/${jobId}`);

// Signals UI (issue #38): a company's activity timeline, and the cross-company
// "What's new" feed (filterable by kind/topic). Both newest-first.
export const fetchCompanySignals = (name: string, limit = 20) =>
  getJson<Signal[]>(`/companies/${encodeURIComponent(name)}/signals?limit=${limit}`);

export const fetchSignals = (params: { kind?: string; topic?: string; limit?: number } = {}) => {
  const qs = new URLSearchParams();
  if (params.kind) qs.set("kind", params.kind);
  if (params.topic) qs.set("topic", params.topic);
  if (params.limit) qs.set("limit", String(params.limit));
  const q = qs.toString();
  return getJson<Signal[]>(`/signals${q ? `?${q}` : ""}`);
};

// Weekly digest (issue #51): browse stored digests newest-first, then one
// digest's full grouped payload. Read-only; generated by a scheduled job.
export const fetchDigests = (limit = 52) =>
  getJson<import("./types").DigestSummaryRow[]>(`/digests?limit=${limit}`);

export const fetchDigest = (id: string) =>
  getJson<import("./types").Digest>(`/digests/${encodeURIComponent(id)}`);

// Potential-acquirer analysis (#44): ranked candidate acquirers for one company
// (drawer section), and the space-level most-active-acquirers view (optional topic
// filter) for the M&A page. Read-only.
export const fetchPotentialAcquirers = (name: string) =>
  getJson<import("./types").AcquirerCandidate[]>(
    `/companies/${encodeURIComponent(name)}/potential-acquirers`,
  );

export const fetchActiveAcquirers = (topic?: string) => {
  const qs = topic ? `?topic=${encodeURIComponent(topic)}` : "";
  return getJson<import("./types").ActiveAcquirer[]>(`/ma/active-acquirers${qs}`);
};
