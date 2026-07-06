import type { ReactNode } from "react";
import { useEffect, useState } from "react";
import type { User } from "firebase/auth";
import { AUTH_ENABLED, onAuthChange, signIn } from "./firebase";

/** Blocks the app behind Google sign-in when auth is enabled. No-op locally. */
export function AuthGate({ children }: { children: ReactNode }) {
  const [user, setUser] = useState<User | null>(null);
  const [ready, setReady] = useState(false);

  useEffect(() => onAuthChange((u) => {
    setUser(u);
    setReady(true);
  }), []);

  if (!ready) return null;

  if (AUTH_ENABLED && !user) {
    return (
      <div className="signin">
        <h1>Nebula</h1>
        <p className="muted">Private research graph — sign in to continue.</p>
        <button onClick={signIn}>Sign in with Google</button>
      </div>
    );
  }
  return <>{children}</>;
}
