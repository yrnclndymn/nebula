import type { ReactNode } from "react";

// The routed-page shell (#151): title bar + naturally-scrolling content column.
// Replaces the hand-rolled `.backfill-overlay`/`.backfill-modal` full-screen
// modals — pages scroll like pages, so tall content can never trap the user
// the way the centered-flex modal did (#145).
export function Page({ title, children }: { title: ReactNode; children: ReactNode }) {
  return (
    <section className="page">
      <div className="page-head">
        <strong>{title}</strong>
      </div>
      {children}
    </section>
  );
}
