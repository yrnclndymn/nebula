import { useCallback, useEffect, useState } from "react";
import { fetchCompanySignals } from "./api";
import { SignalCaptureButton } from "./SignalCaptureButton";
import { SignalList } from "./SignalTimeline";
import type { Signal } from "./types";

// The company drawer's "Signals" section (issue #38): the own-site capture button
// (#34) plus the activity timeline of signals mentioning this company, newest-first.
// Capture writes new Signal nodes, so when a capture job finishes we refetch the
// timeline (via the button's onDone) to fold in whatever it found.
export function SignalsSection({ name, hasWebsite }: { name: string; hasWebsite: boolean }) {
  const [signals, setSignals] = useState<Signal[]>([]);
  const [loaded, setLoaded] = useState(false);

  const load = useCallback(() => {
    let alive = true;
    fetchCompanySignals(name)
      .then((s) => alive && setSignals(s))
      .catch(() => alive && setSignals([]))
      .finally(() => alive && setLoaded(true));
    return () => {
      alive = false;
    };
  }, [name]);

  useEffect(() => load(), [load]);

  // Nothing to show and no way to capture: hide the section entirely.
  if (loaded && signals.length === 0 && !hasWebsite) return null;

  return (
    <div className="chips-block signals-section">
      <span className="field-label">
        Signals <span className="muted">({signals.length})</span>
      </span>
      {hasWebsite && <SignalCaptureButton key={name} name={name} onDone={load} />}
      {signals.length > 0 ? (
        <SignalList signals={signals} />
      ) : (
        loaded && <div className="muted small">No signals captured yet.</div>
      )}
    </div>
  );
}
