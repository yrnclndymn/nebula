// Crawled/researched URLs are untrusted input — render a link only when the
// scheme is http(s), so a hostile javascript:/data: value can never become a
// clickable href (PR #121 review). Shared by every surface that links out to
// signal/deal/citation sources.
export function isHttpUrl(url: string | null | undefined): url is string {
  if (!url) return false;
  try {
    const u = new URL(url);
    return u.protocol === "http:" || u.protocol === "https:";
  } catch {
    return false;
  }
}
