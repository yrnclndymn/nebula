import type { Signal } from "./types";
import { signalKindLabel } from "./types";
import { isHttpUrl } from "./urls";
import { signalWhen } from "./dates";

// Shared renderer for a list of signals (issue #38): the company drawer's activity
// timeline and the cross-company "What's new" feed both use it. Signals come from
// crawled feeds — untrusted — so titles/summaries render as plain (auto-escaped)
// text nodes and a signal only links out when its URL is http(s). Older signals may
// predate the backend's http(s) guard, so we re-check here too.

// A stable-ish key: the canonical URL is unique per signal; fall back to the title.
function signalKey(s: Signal, i: number): string {
  return s.url || s.title || String(i);
}

export function SignalList({
  signals,
  showCompanies = false,
}: {
  signals: Signal[];
  showCompanies?: boolean;
}) {
  return (
    <ul className="signal-list">
      {signals.map((s, i) => {
        const when = signalWhen(s);
        const title = s.title || s.url || "(untitled)";
        return (
          <li key={signalKey(s, i)} className="signal-item">
            <div className="signal-head">
              <span className={`signal-kind kind-${s.kind}`}>{signalKindLabel(s.kind)}</span>
              {isHttpUrl(s.url) ? (
                <a className="signal-title" href={s.url} target="_blank" rel="noreferrer">
                  {title} ↗
                </a>
              ) : (
                <span className="signal-title">{title}</span>
              )}
            </div>
            {when && <div className="signal-when muted small">{when}</div>}
            {s.summary && <p className="signal-summary">{s.summary}</p>}
            {showCompanies && s.companies.length > 0 && (
              <div className="chips signal-companies">
                {s.companies.map((c) => (
                  <span key={c} className="chip">
                    {c}
                  </span>
                ))}
              </div>
            )}
            {s.sources.filter(isHttpUrl).length > 0 && (
              <div className="muted small signal-sources">
                {s.sources.filter(isHttpUrl).map((src, j) => (
                  <a key={src} href={src} target="_blank" rel="noreferrer">
                    source {j + 1} ↗
                  </a>
                ))}
              </div>
            )}
          </li>
        );
      })}
    </ul>
  );
}
