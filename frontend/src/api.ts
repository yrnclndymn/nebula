import type { CompanyDetail, CompanyRow } from "./types";

// Backend base URL. Override in production via VITE_API_BASE.
export const API_BASE = import.meta.env.VITE_API_BASE ?? "http://localhost:8080";

async function getJson<T>(path: string): Promise<T> {
  const resp = await fetch(`${API_BASE}${path}`);
  if (!resp.ok) throw new Error(`${path} → ${resp.status}`);
  return resp.json() as Promise<T>;
}

export const fetchCompanies = () => getJson<CompanyRow[]>("/companies");
export const fetchCompany = (name: string) =>
  getJson<CompanyDetail>(`/companies/${encodeURIComponent(name)}`);
export const fetchTopics = () => getJson<string[]>("/topics");
export const fetchCompanyTypes = () => getJson<string[]>("/company-types");
