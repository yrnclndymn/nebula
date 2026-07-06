// Firebase auth, behind a flag. When VITE_AUTH_ENABLED !== "true" (local dev), no
// Firebase is initialized and the app renders without a login — so local dev and
// the build need no Firebase config. In prod, set the VITE_FIREBASE_* vars.
import { initializeApp } from "firebase/app";
import {
  getAuth,
  getRedirectResult,
  GoogleAuthProvider,
  onAuthStateChanged,
  signInWithPopup,
  signInWithRedirect,
  signOut as fbSignOut,
  type Auth,
  type User,
} from "firebase/auth";

export const AUTH_ENABLED = import.meta.env.VITE_AUTH_ENABLED === "true";

let auth: Auth | null = null;
if (AUTH_ENABLED) {
  const app = initializeApp({
    apiKey: import.meta.env.VITE_FIREBASE_API_KEY,
    authDomain: import.meta.env.VITE_FIREBASE_AUTH_DOMAIN,
    projectId: import.meta.env.VITE_FIREBASE_PROJECT_ID,
    appId: import.meta.env.VITE_FIREBASE_APP_ID,
  });
  auth = getAuth(app);
}

/** Subscribe to auth state. When auth is off, immediately reports a local user. */
export function onAuthChange(cb: (user: User | null) => void): () => void {
  if (!auth) {
    cb({ email: "local-dev" } as User);
    return () => {};
  }
  return onAuthStateChanged(auth, cb);
}

/** Sign in with Google. Surfaces errors (caller shows them); falls back to a
 * full-page redirect if the popup is blocked. */
export async function signIn(): Promise<void> {
  if (!auth) return;
  const provider = new GoogleAuthProvider();
  try {
    await signInWithPopup(auth, provider);
  } catch (err) {
    const code = (err as { code?: string })?.code ?? "";
    if (code === "auth/popup-blocked" || code === "auth/cancelled-popup-request") {
      await signInWithRedirect(auth, provider);
      return;
    }
    throw err;
  }
}

/** After a redirect sign-in, surface any error (e.g. unauthorized-domain). */
export async function redirectError(): Promise<string | null> {
  if (!auth) return null;
  try {
    await getRedirectResult(auth);
    return null;
  } catch (err) {
    return (err as { message?: string })?.message ?? String(err);
  }
}

export function signOutUser(): void {
  if (auth) fbSignOut(auth);
}

export async function getIdToken(): Promise<string | null> {
  if (!auth?.currentUser) return null;
  return auth.currentUser.getIdToken();
}
