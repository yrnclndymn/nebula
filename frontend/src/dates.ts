// Shared date-labelling helpers (#152) — the same two functions had been
// copy-pasted across the news surfaces and the drawers.

// A parseable date renders localised; otherwise keep the raw string (or nothing).
// Used by the digest and M&A pages for deal/signal timestamps.
export function whenLabel(raw: string | null | undefined): string | null {
  if (!raw) return null;
  const t = Date.parse(raw);
  return Number.isNaN(t) ? raw : new Date(t).toLocaleDateString();
}

// Best "when" for a signal-like record: prefer the parsed publish date, fall back
// to the raw feed string, then to the capture time (prefixed). Shared by the
// cross-company signal timeline and the person page's linked-signals list — both
// carry the same three optional fields.
export function signalWhen(s: {
  publishedAt?: string | null;
  publishedAtRaw?: string | null;
  capturedAt?: string | null;
}): string | null {
  if (s.publishedAt) {
    const t = Date.parse(s.publishedAt);
    if (!Number.isNaN(t)) return new Date(t).toLocaleDateString();
  }
  if (s.publishedAtRaw) return s.publishedAtRaw;
  if (s.capturedAt) {
    const t = Date.parse(s.capturedAt);
    if (!Number.isNaN(t)) return `captured ${new Date(t).toLocaleDateString()}`;
  }
  return null;
}
