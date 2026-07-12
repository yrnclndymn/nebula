import { useEffect, useRef, useState } from "react";
import { getSignalCapture, startSignalCapture } from "./api";

// Trigger own-site signal capture (issue #34) from the company drawer: one
// button → durable job → captured/new counts when done. Signals aren't surfaced
// in the UI yet (issue #38); until then the outcome line here and the activity
// page are the feedback.

type Phase = "idle" | "running" | "done" | "error";

export function SignalCaptureButton({ name }: { name: string }) {
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
