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
