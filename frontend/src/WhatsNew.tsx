import { useEffect, useState } from "react";
import { fetchSignals } from "./api";
import { Page } from "./Page";
import { SignalList } from "./SignalTimeline";
import type { Signal } from "./types";
import { SIGNAL_KINDS, signalKindLabel } from "./types";
import { ClearFiltersButton, FilterBar, FilterSelect } from "./FilterBar";

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

        <FilterBar variant="whatsnew">
          <FilterSelect
            value={kind}
            onChange={setKind}
            allLabel="All kinds"
            options={SIGNAL_KINDS.map((k) => ({ value: k, label: signalKindLabel(k) }))}
          />
          <FilterSelect
            value={topic}
            onChange={setTopic}
            allLabel="All topics"
            options={topics.map((t) => ({ value: t, label: t }))}
          />
          {(kind || topic) && (
            <ClearFiltersButton
              onClear={() => {
                setKind("");
                setTopic("");
              }}
            />
          )}
        </FilterBar>

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
