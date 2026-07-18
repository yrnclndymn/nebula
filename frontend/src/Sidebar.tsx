import { NavLink } from "react-router-dom";
import { AUTH_ENABLED, signOutUser } from "./firebase";
import { usePendingReview } from "./usePendingReview";

// Left navigation (#151): the three flows. The Review flow carries a badge with
// the count of items awaiting a commit decision (#153).
const FLOWS = [
  { to: "/companies", label: "Companies", icon: "🔭" },
  { to: "/review", label: "Review", icon: "📥" },
  { to: "/news", label: "News", icon: "🆕" },
];

export function Sidebar({
  chatOpen,
  onToggleChat,
}: {
  chatOpen: boolean;
  onToggleChat: () => void;
}) {
  const pendingReview = usePendingReview();
  return (
    <nav className="sidebar">
      <h1 className="sidebar-brand">
        Nebula <span className="sub">research graph</span>
      </h1>
      {FLOWS.map((f) => (
        <NavLink
          key={f.to}
          to={f.to}
          className={({ isActive }) => (isActive ? "sidebar-link active" : "sidebar-link")}
        >
          <span className="sidebar-icon">{f.icon}</span> {f.label}
          {f.to === "/review" && pendingReview > 0 && (
            <span className="sidebar-badge">{pendingReview}</span>
          )}
        </NavLink>
      ))}
      <div className="sidebar-spacer" />
      <button
        className={chatOpen ? "sidebar-link active" : "sidebar-link"}
        onClick={onToggleChat}
      >
        <span className="sidebar-icon">💬</span> Assistant
      </button>
      {AUTH_ENABLED && (
        <button className="sidebar-link sidebar-signout" onClick={signOutUser}>
          Sign out
        </button>
      )}
    </nav>
  );
}
