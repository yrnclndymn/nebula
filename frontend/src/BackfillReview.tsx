import { useEffect, useState } from "react";
import { commitBackfill, getBackfill } from "./api";
import type { Backfill } from "./types";
import { formatCustom } from "./types";

export function BackfillCard({
  job,
  onReview,
}: {
  job: { job_id: string; field: string; total: number };
  onReview: (full: Backfill) => void;
}) {
  const [full, setFull] = useState<Backfill | null>(null);

  useEffect(() => {
    let stop = false;
    const poll = async () => {
      try {
        const b = await getBackfill(job.job_id);
        if (!stop) setFull(b);
        if (b.status === "ready") return;
      } catch {
        /* keep polling */
      }
      if (!stop) setTimeout(poll, 2500);
    };
    poll();
    return () => {
      stop = true;
    };
  }, [job.job_id]);

  const done = full?.done ?? 0;
  const ready = full?.status === "ready";
  const withValues = full ? full.rows.filter((r) => formatCustom(r.value) !== "—").length : 0;

  return (
    <div className="proposal">
      <div className="proposal-head">
        <strong>Back-fill: {job.field}</strong>
      </div>
      {!ready ? (
        <div className="muted">
          🔎 researching {done}/{job.total} companies…
        </div>
      ) : (
        <>
          <div className="muted">
            {withValues} of {full!.total} companies have a value.
          </div>
          <div className="proposal-actions">
            <button className="commit" onClick={() => onReview(full!)}>
              Review &amp; commit
            </button>
          </div>
        </>
      )}
    </div>
  );
}

export function BackfillModal({
  job,
  onClose,
  onCommitted,
}: {
  job: Backfill;
  onClose: () => void;
  onCommitted: (count: number) => void;
}) {
  const withValue = job.rows.filter((r) => formatCustom(r.value) !== "—");
  const [selected, setSelected] = useState<Set<string>>(
    () => new Set(withValue.map((r) => r.company)),
  );
  const [committing, setCommitting] = useState(false);

  function toggle(company: string) {
    setSelected((s) => {
      const next = new Set(s);
      if (next.has(company)) next.delete(company);
      else next.add(company);
      return next;
    });
  }

  async function commit() {
    setCommitting(true);
    try {
      const res = await commitBackfill(job.job_id, [...selected]);
      onCommitted(res.committed ?? 0);
    } catch {
      setCommitting(false);
    }
  }

  return (
    <div className="backfill-overlay" onClick={onClose}>
      <div className="backfill-modal" onClick={(e) => e.stopPropagation()}>
        <div className="backfill-head">
          <strong>Back-fill review — {job.field.label}</strong>
          <button className="drawer-close" onClick={onClose} aria-label="Close">
            ×
          </button>
        </div>
        <div className="backfill-table-wrap">
          <table>
            <thead>
              <tr>
                <th></th>
                <th>Company</th>
                <th>{job.field.label}</th>
                <th>Source</th>
              </tr>
            </thead>
            <tbody>
              {job.rows.map((r) => {
                const hasValue = formatCustom(r.value) !== "—";
                return (
                  <tr key={r.company} className={hasValue ? "" : "muted"}>
                    <td>
                      <input
                        type="checkbox"
                        disabled={!hasValue}
                        checked={selected.has(r.company)}
                        onChange={() => toggle(r.company)}
                      />
                    </td>
                    <td className="name">{r.company}</td>
                    <td>{formatCustom(r.value)}</td>
                    <td className="muted">
                      {r.source ? (
                        <a href={r.source} target="_blank" rel="noreferrer">
                          ↗
                        </a>
                      ) : (
                        "—"
                      )}
                    </td>
                  </tr>
                );
              })}
            </tbody>
          </table>
        </div>
        <div className="backfill-foot">
          <button className="commit" onClick={commit} disabled={committing || selected.size === 0}>
            {committing ? "committing…" : `Commit ${selected.size} selected`}
          </button>
          <button className="discard" onClick={onClose}>
            Cancel
          </button>
        </div>
      </div>
    </div>
  );
}
