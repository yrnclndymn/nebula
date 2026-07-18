import { useEffect, useState } from "react";
import { fetchSignals } from "./api";
import { Page } from "./Page";
import { SignalList } from "./SignalTimeline";
import type { Signal } from "./types";
import { SIGNAL_KINDS, signalKindLabel } from "./types";

const FEED_LIMIT = 100;

// The "What's new" feed (issue #38): recent signals across every company,
// newest-first, filterable by kind (news/blog/event) and topic. A cross-company
// counterpart to the drawer's per-company timeline. Refetches server-side whenever
// a filter changes (the topic/kind narrowing lives in the graph query).
export function WhatsNewPage({ topics }: { topics: string[] }) {
  const [signals, setSignals] = useState<Signal[]>([]);
  const [kind, setKind] = useState("");
  const [topic, setTopic] = useState("");
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState<string | null>(null);

  useEffect(() => {
    let stop = false;
    setLoading(true);
    setError(null);
    fetchSignals({ kind: kind || undefined, topic: topic || undefined, limit: FEED_LIMIT })
      .then((s) => !stop && setSignals(s))
      .catch((e) => !stop && setError(String(e)))
      .finally(() => !stop && setLoading(false));
    return () => {
      stop = true;
    };
  }, [kind, topic]);

  return (
    <Page title={<>What&rsquo;s new</>}>

        <div className="filters whatsnew-filters">
          <select value={kind} onChange={(e) => setKind(e.target.value)}>
            <option value="">All kinds</option>
            {SIGNAL_KINDS.map((k) => (
              <option key={k} value={k}>
                {signalKindLabel(k)}
              </option>
            ))}
          </select>
          <select value={topic} onChange={(e) => setTopic(e.target.value)}>
            <option value="">All topics</option>
            {topics.map((t) => (
              <option key={t}>{t}</option>
            ))}
          </select>
          {(kind || topic) && (
            <button
              className="clear"
              onClick={() => {
                setKind("");
                setTopic("");
              }}
            >
              Clear
            </button>
          )}
        </div>

        <div className="backfill-table-wrap">
          {error ? (
            <div className="proposal-err">⚠ couldn&rsquo;t load signals: {error}</div>
          ) : loading ? (
            <div className="muted" style={{ padding: "1rem" }}>
              loading signals…
            </div>
          ) : signals.length === 0 ? (
            <p className="muted" style={{ padding: "1rem" }}>
              No signals yet. Capture a company&rsquo;s signals from its drawer to see activity here.
            </p>
          ) : (
            <SignalList signals={signals} showCompanies />
          )}
        </div>

    </Page>
  );
}
