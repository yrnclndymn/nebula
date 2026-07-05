import type { CompanyDetail, CompanyRow, Proposal } from "./types";

// Backend base URL. Override in production via VITE_API_BASE.
export const API_BASE = import.meta.env.VITE_API_BASE ?? "http://localhost:8080";

async function getJson<T>(path: string): Promise<T> {
  const resp = await fetch(`${API_BASE}${path}`);
  if (!resp.ok) throw new Error(`${path} → ${resp.status}`);
  return resp.json() as Promise<T>;
}

async function postJson<T>(path: string, body: unknown): Promise<T> {
  const resp = await fetch(`${API_BASE}${path}`, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify(body),
  });
  if (!resp.ok) throw new Error(`${path} → ${resp.status}`);
  return resp.json() as Promise<T>;
}

export const sendChat = (sessionId: string, message: string) =>
  postJson<{ reply: string; proposals: Proposal[] }>("/chat", {
    session_id: sessionId,
    message,
  });

export const setKind = (name: string, kind: string | null) =>
  fetch(`${API_BASE}/companies/${encodeURIComponent(name)}/kind`, {
    method: "PATCH",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ kind }),
  }).then((r) => {
    if (!r.ok) throw new Error(`kind → ${r.status}`);
    return r.json() as Promise<{ name: string; kind: string | null }>;
  });

export const getProposal = (proposalId: string) =>
  getJson<Proposal>(`/proposals/${encodeURIComponent(proposalId)}`);

export const commitProposal = (proposalId: string) =>
  postJson<{ committed?: string; error?: string }>("/proposals/commit", {
    proposal_id: proposalId,
  });

export const fetchCompanies = () => getJson<CompanyRow[]>("/companies");
export const fetchCompany = (name: string) =>
  getJson<CompanyDetail>(`/companies/${encodeURIComponent(name)}`);
export const fetchTopics = () => getJson<string[]>("/topics");
export const fetchCompanyTypes = () => getJson<string[]>("/company-types");
export const fetchFields = () => getJson<import("./types").FieldDef[]>("/fields");
