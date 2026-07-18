import { useEffect, useState } from "react";
import { fetchPotentialAcquirers } from "./api";
import type { AcquirerCandidate, AcquirerWhy } from "./types";
import { isHttpUrl } from "./urls";

// Potential acquirers (#44): a self-contained drawer section that fetches ranked
// candidate acquirers for the open company and renders each with its explainable
// why-reasons; deal facts link back to their source.

function AcquirerDeals({ deals }: { deals: { target: string; source: string | null }[] }) {
  return (
    <>
      {deals.map((deal, i) => (
        <span key={deal.target}>
          {i > 0 && ", "}
          {isHttpUrl(deal.source) ? (
            <a href={deal.source} target="_blank" rel="noreferrer">
              {deal.target} ↗
            </a>
          ) : (
            deal.target
          )}
        </span>
      ))}
    </>
  );
}

// One why-reason as a list line. Deal-bearing signals show the acquired companies
// linking to their source; overlap signals list the shared names.
function AcquirerReason({ why }: { why: AcquirerWhy }) {
  const d = why.detail;
  if (why.signal === "acquired-in-topic" || why.signal === "acquired-same-kind") {
    const label =
      why.signal === "acquired-in-topic"
        ? `Acquired ${d.count} in this space`
        : `Acquired ${d.count} of the same kind${d.kind ? ` (${d.kind})` : ""}`;
    return (
      <li>
        <span className="acq-reason">{label}</span>{" "}
        <AcquirerDeals deals={d.deals ?? []} />
      </li>
    );
  }
  if (why.signal === "direct-partner")
    return (
      <li>
        <span className="acq-reason">Already a partner</span>
      </li>
    );
  if (why.signal === "shared-partners")
    return (
      <li>
        <span className="acq-reason">Shared partners</span>{" "}
        <span className="muted">{(d.partners ?? []).join(", ")}</span>
      </li>
    );
  if (why.signal === "shared-clients")
    return (
      <li>
        <span className="acq-reason">Shared clients</span>{" "}
        <span className="muted">{(d.clients ?? []).join(", ")}</span>
      </li>
    );
  if (why.signal === "active-acquirer")
    return (
      <li>
        <span className="muted">{d.total_acquisitions} acquisitions on record</span>
      </li>
    );
  return null;
}

export function PotentialAcquirersSection({ name }: { name: string }) {
  const [candidates, setCandidates] = useState<AcquirerCandidate[]>([]);
  const [loaded, setLoaded] = useState(false);

  useEffect(() => {
    let alive = true;
    setLoaded(false);
    fetchPotentialAcquirers(name)
      .then((c) => alive && setCandidates(c))
      .catch(() => alive && setCandidates([]))
      .finally(() => alive && setLoaded(true));
    return () => {
      alive = false;
    };
  }, [name]);

  // Only researched companies with acquisition ties surface anything; hide otherwise.
  if (!loaded || candidates.length === 0) return null;

  return (
    <div className="chips-block acquirers-section">
      <span className="field-label">
        Potential acquirers <span className="muted">({candidates.length})</span>
      </span>
      <ul className="acquirers">
        {candidates.map((c) => (
          <li key={c.acquirer} className="acquirer">
            <span className="acquirer-name">{c.acquirer}</span>
            <ul className="acquirer-why">
              {c.why.map((w, i) => (
                <AcquirerReason key={`${w.signal}-${i}`} why={w} />
              ))}
            </ul>
          </li>
        ))}
      </ul>
    </div>
  );
}
