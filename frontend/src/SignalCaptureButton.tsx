import { useEffect, useRef, useState } from "react";
import { getSignalCapture, startSignalCapture } from "./api";

// Trigger own-site signal capture (issue #34) from the company drawer: one
// button → durable job → captured/new counts when done. Lives inside the drawer's
// Signals section (issue #38); `onDone` lets that section refresh its timeline once
// a capture completes, so newly-captured items appear without reopening the drawer.

type Phase = "idle" | "running" | "done" | "error";

export function SignalCaptureButton({ name, onDone }: { name: string; onDone?: () => void }) {
  const [phase, setPhase] = useState<Phase>("idle");
  const [outcome, setOutcome] = useState<string>("");
  const stop = useRef(false);

  // The drawer unmounts this on close/company-switch; stop the poll loop then.
  useEffect(() => {
    stop.current = false;
    return () => {
      stop.current = true;
    };
  }, []);

  async function poll(jobId: string) {
    try {
      const job = await getSignalCapture(jobId);
      if (stop.current) return;
      if (job.status === "done") {
        setOutcome(job.outcome ?? `captured ${job.captured ?? 0} items`);
        setPhase("done");
        onDone?.();
        return;
      }
      if (job.status === "error") {
        setOutcome(job.error ?? "capture failed");
        setPhase("error");
        return;
      }
    } catch {
      /* keep polling through transient errors */
    }
    if (!stop.current) setTimeout(() => poll(jobId), 2500);
  }

  async function capture() {
    setPhase("running");
    setOutcome("");
    try {
      const res = await startSignalCapture(name);
      poll(res.job_id);
    } catch {
      setOutcome("could not start capture");
      setPhase("error");
    }
  }

  return (
    <div className="capture-signals">
      <button type="button" onClick={capture} disabled={phase === "running"}>
        {phase === "running" ? "Capturing…" : "📡 Capture signals"}
      </button>
      {outcome && <span className={phase === "error" ? "muted error" : "muted"}> {outcome}</span>}
    </div>
  );
}
