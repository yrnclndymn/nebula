export interface CompanyRow {
  name: string;
  priority: string | null;
  about: string | null;
  website: string | null;
  linkedin: string | null;
  hqLocation: string | null;
  headcount: number | null;
  estimatedRevenue: string | null;
  yearFounded: number | null;
  funding: string | null;
  notes: string | null;
  origin: string | null;
  topics: string[];
  companyTypes: string[];
  partnerCount: number;
  clientCount: number;
  leaderCount: number;
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

export interface Proposal {
  proposal_id: string;
  name: string;
  exists: boolean;
  summary: string;
  record: ProposalRecord;
}

export interface CompanyDetail {
  name: string;
  priority: string | null;
  about: string | null;
  website: string | null;
  linkedin: string | null;
  hqLocation: string | null;
  headcount: number | null;
  estimatedRevenue: string | null;
  yearFounded: number | null;
  funding: string | null;
  notes: string | null;
  origin: string | null;
  topics: string[];
  companyTypes: string[];
  partners: string[];
  clients: string[];
  leadership: Leader[];
  citations: Citation[];
}
