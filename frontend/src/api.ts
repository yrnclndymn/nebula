import { getIdToken } from "./firebase";
import type { Backfill, CompanyDetail, CompanyRow, Proposal } from "./types";

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
  postJson<{ reply: string; proposals: Proposal[]; backfills: { job_id: string; field: string; total: number }[] }>(
    "/chat",
    { session_id: sessionId, message },
  );

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

export const fetchCompanies = () => getJson<CompanyRow[]>("/companies");
export const fetchCompany = (name: string) =>
  getJson<CompanyDetail>(`/companies/${encodeURIComponent(name)}`);
export const fetchTopics = () => getJson<string[]>("/topics");
export const fetchCompanyTypes = () => getJson<string[]>("/company-types");
export const fetchFields = () => getJson<import("./types").FieldDef[]>("/fields");
export const fetchCountries = () => getJson<string[]>("/countries");
