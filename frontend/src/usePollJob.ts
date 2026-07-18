import { useEffect, useRef } from "react";

// Shared polling primitive (#152) — replaces the ~7 hand-rolled poll loops that
// each re-implemented "keep hitting the API on an interval while some work is
// still in flight, then stop, and don't touch state after the component is gone".
//
// The surfaces came in two shapes:
//   • declarative — a `pending`/`active` flag already lives in state, and a
//     `setInterval` runs while it holds (ProposalCard, the acquisition cards,
//     the activity board, the backlog);
//   • imperative — a button starts a durable job and a recursive `setTimeout`
//     polls that job id until it settles, guarded by a `stop` ref
//     (DiscoveryPanel, SignalCaptureButton, the person expertise section).
//
// Both reduce to: while `active`, run `tick` every `intervalMs`; on cleanup mark
// the run cancelled so an in-flight tick can bail before calling setState. The
// imperative surfaces additionally want the first poll to fire immediately
// (`leading`) instead of waiting a full interval — so lifting their job id into
// state and flipping `active` reproduces the old "poll right after start"
// behaviour with no visible delay.
//
// `tick` receives a `cancelled()` guard: check it after every `await` before
// touching state, exactly as the old `stop.current` refs did.
export function usePollJob(
  active: boolean,
  tick: (cancelled: () => boolean) => void | Promise<void>,
  options: { intervalMs?: number; leading?: boolean } = {},
): void {
  const { intervalMs = 2500, leading = false } = options;

  // Keep the latest tick without re-arming the interval every render — the
  // interval's lifetime is driven only by `active`/`intervalMs`/`leading`.
  const saved = useRef(tick);
  useEffect(() => {
    saved.current = tick;
  });

  useEffect(() => {
    if (!active) return;
    let cancelled = false;
    const isCancelled = () => cancelled;
    if (leading) void saved.current(isCancelled);
    const iv = setInterval(() => void saved.current(isCancelled), intervalMs);
    return () => {
      cancelled = true;
      clearInterval(iv);
    };
  }, [active, intervalMs, leading]);
}
