import { useEffect, useState } from "react";
import { fetchThesisRules } from "./api";
import type { ThesisRule } from "./types";
import { isHttpUrl } from "./urls";
import { whenLabel } from "./dates";
import { thesisPair, originLabel, confidenceLabel } from "./thesis";

// Market thesis (#195, epic #192): a read-only panel above the M&A deals table
// showing the stored acquisition-thesis model — the *why* behind the potential
// -acquirer ranking (#194). Each rule renders its human-readable statement, its
// acquirer→target kind shape (+ optional qualifier), a confidence, an origin badge
// (Maintainer / Reviewer), when it was last updated, and its supporting evidence as
// http(s)-guarded citation links. Read-only: revisions arrive via the evidence loop
// (#196), so there are no edit affordances here. Statements/URLs originate from graph
// data / crawled deal citations (untrusted), so text renders escaped and a link only
// appears when the URL is http(s). Self-hides nothing — it always explains its state
// (loading / error / empty-with-seed-hint / rules), since the thesis is the panel's
// whole point.

function ThesisCard({ rule }: { rule: ThesisRule }) {
  const updated = whenLabel(rule.updated_at);
  const sources = rule.sources.filter(isHttpUrl);
  return (
    <li className="thesis-rule">
      <div className="thesis-statement">{rule.statement}</div>
      <div className="thesis-meta">
        <span className="thesis-pair muted small">{thesisPair(rule)}</span>
        {rule.qualifier && <span className="thesis-qualifier muted small">· {rule.qualifier}</span>}
        <span className={`origin origin-${rule.origin}`}>{originLabel(rule.origin)}</span>
      </div>
      <div className="thesis-confidence" title={`Confidence ${confidenceLabel(rule.confidence)}`}>
        <span className="muted small">Confidence</span>
        <span className="thesis-bar" aria-hidden="true">
          <span
            className="thesis-bar-fill"
            style={{ width: confidenceLabel(rule.confidence) }}
          />
        </span>
        <span className="small">{confidenceLabel(rule.confidence)}</span>
      </div>
      <div className="thesis-evidence muted small">
        {rule.evidence_count === 0 ? (
          <span>No supporting deals yet</span>
        ) : (
          <>
            <span>
              {rule.evidence_count} supporting deal{rule.evidence_count === 1 ? "" : "s"}
            </span>
            {sources.length > 0 && (
              <span className="thesis-sources">
                {sources.map((url, i) => (
                  <a key={url} href={url} target="_blank" rel="noreferrer">
                    source {i + 1} ↗
                  </a>
                ))}
              </span>
            )}
          </>
        )}
      </div>
      {updated && <div className="thesis-updated muted small">Updated {updated}</div>}
    </li>
  );
}

export function ThesisPanel() {
  const [rules, setRules] = useState<ThesisRule[]>([]);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState<string | null>(null);

  useEffect(() => {
    let stop = false;
    fetchThesisRules()
      .then((r) => !stop && setRules(r))
      .catch((e) => !stop && setError(String(e)))
      .finally(() => !stop && setLoading(false));
    return () => {
      stop = true;
    };
  }, []);

  return (
    <div className="thesis-panel">
      <div className="thesis-head">
        <strong>📐 Market thesis</strong>
        <span className="muted small">
          The model of who acquires whom — the reasoning behind acquirer suggestions.
        </span>
      </div>
      {error ? (
        <div className="proposal-err">⚠ couldn&rsquo;t load the thesis: {error}</div>
      ) : loading ? (
        <div className="muted" style={{ padding: "0.5rem 0" }}>
          loading thesis…
        </div>
      ) : rules.length === 0 ? (
        <p className="muted small" style={{ padding: "0.25rem 0" }}>
          No thesis rules yet. Seed the maintainer&rsquo;s market thesis with{" "}
          <code>make seed-thesis</code>.
        </p>
      ) : (
        <ul className="thesis-rules">
          {rules.map((r) => (
            <ThesisCard key={r.rule_key} rule={r} />
          ))}
        </ul>
      )}
    </div>
  );
}
