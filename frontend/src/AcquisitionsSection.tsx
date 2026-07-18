import { useEffect, useState } from "react";
import { fetchCompanyAcquisitions } from "./api";
import { AcquisitionProposalsPanel } from "./AcquisitionProposals";
import type { Acquisition } from "./types";
import { isHttpUrl } from "./urls";

// Acquisitions (#45): a self-contained drawer section that fetches this company's
// ACQUIRED edges (both directions) and splits them into deals it *made* (it is
// the acquirer) vs deals where it was *acquired* (it is the target). Each deal's
// `source` and the amount's `amount_source` render as citation links, http(s)
// only. An amount is shown ONLY next to its `amount_source` link — an uncited
// figure is never surfaced (the repo's provenance guarantee). Hides itself when
// there are no deals, like SignalsSection.

function DealRow({ deal, counterparty }: { deal: Acquisition; counterparty: string }) {
  const when = deal.announced_at || deal.closed_at;
  return (
    <li className="deal-item">
      <div className="deal-head">
        <strong>{counterparty}</strong>
        {when && <span className="muted small"> · {when}</span>}
      </div>
      {deal.amount && isHttpUrl(deal.amount_source) && (
        <div className="deal-amount">
          {deal.currency ? `${deal.currency} ` : ""}
          {deal.amount}{" "}
          <a href={deal.amount_source} target="_blank" rel="noreferrer">
            source ↗
          </a>
        </div>
      )}
      {deal.thesis && <div className="deal-thesis muted small">{deal.thesis}</div>}
      {isHttpUrl(deal.source) && (
        <a className="deal-source small" href={deal.source} target="_blank" rel="noreferrer">
          deal source ↗
        </a>
      )}
    </li>
  );
}

export function AcquisitionsSection({ name }: { name: string }) {
  const [deals, setDeals] = useState<Acquisition[]>([]);
  const [loaded, setLoaded] = useState(false);

  useEffect(() => {
    let alive = true;
    fetchCompanyAcquisitions(name)
      .then((d) => alive && setDeals(d))
      .catch(() => alive && setDeals([]))
      .finally(() => alive && setLoaded(true));
    return () => {
      alive = false;
    };
  }, [name]);

  const made = deals.filter((d) => d.acquirer === name);
  const received = deals.filter((d) => d.target === name);

  // #133: pending/ready acquisition proposals for THIS company sit above the
  // committed edges — the reviewer can commit them here. The panel self-hides when
  // there are none, so the section still disappears when nothing is stored or pending.
  return (
    <>
      <AcquisitionProposalsPanel company={name} heading="Pending acquisition proposals" />
      {loaded && deals.length > 0 && (
        <div className="chips-block acquisitions-section">
          <span className="field-label">
            Acquisitions <span className="muted">({deals.length})</span>
          </span>
          {made.length > 0 && (
            <div className="deal-group">
              <span className="field-sublabel muted small">Acquired ({made.length})</span>
              <ul className="deal-list">
                {made.map((d) => (
                  <DealRow key={`m-${d.target}`} deal={d} counterparty={d.target} />
                ))}
              </ul>
            </div>
          )}
          {received.length > 0 && (
            <div className="deal-group">
              <span className="field-sublabel muted small">Acquired by ({received.length})</span>
              <ul className="deal-list">
                {received.map((d) => (
                  <DealRow key={`r-${d.acquirer}`} deal={d} counterparty={d.acquirer} />
                ))}
              </ul>
            </div>
          )}
        </div>
      )}
    </>
  );
}
