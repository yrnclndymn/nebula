import { useState } from "react";
import { getSignalCapture, startSignalCapture } from "./api";
import { usePollJob } from "./usePollJob";

// Trigger own-site signal capture (issue #34) from the company drawer: one
// button → durable job → captured/new counts when done. Lives inside the drawer's
// Signals section (issue #38); `onDone` lets that section refresh its timeline once
// a capture completes, so newly-captured items appear without reopening the drawer.

type Phase = "idle" | "running" | "done" | "error";

export function SignalCaptureButton({ name, onDone }: { name: string; onDone?: () => void }) {
  const [phase, setPhase] = useState<Phase>("idle");
  const [outcome, setOutcome] = useState<string>("");
  const [jobId, setJobId] = useState<string | null>(null);

  // The drawer keys/unmounts this on company-switch/close, so the hook tears the
  // poll down; poll the capture job until it reports done/error.
  usePollJob(
    phase === "running" && jobId !== null,
    async (cancelled) => {
      if (!jobId) return;
      try {
        const job = await getSignalCapture(jobId);
        if (cancelled()) return;
        if (job.status === "done") {
          setOutcome(job.outcome ?? `captured ${job.captured ?? 0} items`);
          setPhase("done");
          onDone?.();
        } else if (job.status === "error") {
          setOutcome(job.error ?? "capture failed");
          setPhase("error");
        }
      } catch {
        /* keep polling through transient errors */
      }
    },
    { leading: true },
  );

  async function capture() {
    setJobId(null);
    setPhase("running");
    setOutcome("");
    try {
      const res = await startSignalCapture(name);
      setJobId(res.job_id);
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
