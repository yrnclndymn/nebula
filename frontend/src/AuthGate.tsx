import type { ReactNode } from "react";
import { useEffect, useState } from "react";
import type { User } from "firebase/auth";
import { AUTH_ENABLED, onAuthChange, redirectError, signIn } from "./firebase";

/** Blocks the app behind Google sign-in when auth is enabled. No-op locally. */
export function AuthGate({ children }: { children: ReactNode }) {
  const [user, setUser] = useState<User | null>(null);
  const [ready, setReady] = useState(false);
  const [error, setError] = useState<string | null>(null);

  useEffect(() => onAuthChange((u) => {
    setUser(u);
    setReady(true);
  }), []);

  // Surface an error from a redirect-based sign-in, if any.
  useEffect(() => {
    redirectError().then((e) => e && setError(e));
  }, []);

  async function handleSignIn() {
    setError(null);
    try {
      await signIn();
    } catch (err) {
      setError((err as { message?: string })?.message ?? String(err));
    }
  }

  if (!ready) return null;

  if (AUTH_ENABLED && !user) {
    return (
      <div className="signin">
        <h1>Nebula</h1>
        <p className="muted">Private research graph — sign in to continue.</p>
        <button onClick={handleSignIn}>Sign in with Google</button>
        {error && <p className="signin-error">{error}</p>}
      </div>
    );
  }
  return <>{children}</>;
}
