// Firebase auth, behind a flag. When VITE_AUTH_ENABLED !== "true" (local dev), no
// Firebase is initialized and the app renders without a login — so local dev and
// the build need no Firebase config. In prod, set the VITE_FIREBASE_* vars.
import { initializeApp } from "firebase/app";
import {
  getAuth,
  GoogleAuthProvider,
  onAuthStateChanged,
  signInWithPopup,
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

export function signIn(): void {
  if (auth) signInWithPopup(auth, new GoogleAuthProvider());
}

export function signOutUser(): void {
  if (auth) fbSignOut(auth);
}

export async function getIdToken(): Promise<string | null> {
  if (!auth?.currentUser) return null;
  return auth.currentUser.getIdToken();
}
