import { NavLink } from "react-router-dom";
import { AUTH_ENABLED, signOutUser } from "./firebase";

// Left navigation (#151): the three flows. The Review badge (pending count)
// arrives with the inbox story (#153).
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
