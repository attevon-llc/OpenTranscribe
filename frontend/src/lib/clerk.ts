/**
 * Clerk SDK wrapper (cloud edition only).
 *
 * The community/self-host build never touches this module's Clerk paths: every
 * entry point is guarded by {@link isCloudEdition}, and the SDK itself is pulled
 * in via a dynamic `import('@clerk/clerk-js')` so it is *not* bundled into the
 * community build and the publishable key is never required there.
 *
 * Why `@clerk/clerk-js` (vanilla) and not a Svelte SDK: there is no official
 * Clerk Svelte SDK, and this is a static SvelteKit SPA. The vanilla SDK mounts
 * its prebuilt components (`<SignIn/>`, `<UserProfile/>`, `<OrganizationSwitcher/>`)
 * into plain DOM nodes, which is exactly what we need.
 *
 * Components must NOT import the SDK directly — they call the small typed surface
 * here (load / getToken / signOut / mount* / organization helpers).
 */

import { isCloudEdition, clerkPublishableKey } from '$lib/edition';

// Loosely-typed Clerk handles. We intentionally avoid importing Clerk's types at
// module scope (that would pull type-only references into the community build's
// type graph and couple it to the SDK). The dynamic import gives us the real
// instance at runtime; these aliases keep call sites honest without the coupling.
type ClerkInstance = {
  loaded: boolean;
  load: (opts?: Record<string, unknown>) => Promise<void>;
  session?: { getToken: (opts?: { template?: string }) => Promise<string | null> } | null;
  user?: unknown;
  organization?: { id: string; name?: string; slug?: string } | null;
  signOut: (opts?: Record<string, unknown>) => Promise<void>;
  mountSignIn: (node: HTMLElement, props?: Record<string, unknown>) => void;
  unmountSignIn: (node: HTMLElement) => void;
  mountSignUp: (node: HTMLElement, props?: Record<string, unknown>) => void;
  unmountSignUp: (node: HTMLElement) => void;
  mountUserProfile: (node: HTMLElement, props?: Record<string, unknown>) => void;
  unmountUserProfile: (node: HTMLElement) => void;
  openUserProfile: (props?: Record<string, unknown>) => void;
  mountOrganizationSwitcher: (node: HTMLElement, props?: Record<string, unknown>) => void;
  unmountOrganizationSwitcher: (node: HTMLElement) => void;
  addListener: (cb: (payload: unknown) => void) => () => void;
};

let clerkPromise: Promise<ClerkInstance | null> | null = null;

/**
 * Lazily load + initialize the Clerk SDK. Idempotent: repeated calls return the
 * same singleton instance. Returns `null` (never throws) outside the cloud build
 * or when the publishable key is missing, so callers can branch safely.
 */
export async function loadClerk(): Promise<ClerkInstance | null> {
  if (!isCloudEdition) return null;
  if (!clerkPublishableKey) {
    console.error('[clerk] VITE_CLERK_PUBLISHABLE_KEY is not set; Clerk cannot initialize.');
    return null;
  }
  if (clerkPromise) return clerkPromise;

  clerkPromise = (async () => {
    try {
      // Dynamic import keeps Clerk out of the community bundle entirely. The
      // SDK's real types use HTMLDivElement for mount targets; our wrapper uses
      // the looser HTMLElement, so cast through unknown (the structural surface
      // we use is compatible at runtime).
      const mod = await import('@clerk/clerk-js');
      const ClerkCtor = mod.Clerk as unknown as new (key: string) => ClerkInstance;
      const clerk = new ClerkCtor(clerkPublishableKey);
      if (!clerk.loaded) {
        await clerk.load();
      }
      return clerk;
    } catch (err) {
      console.error('[clerk] Failed to load Clerk SDK:', err);
      return null;
    }
  })();

  return clerkPromise;
}

/** The loaded Clerk instance, or `null` if not loaded/applicable. */
export async function getClerk(): Promise<ClerkInstance | null> {
  return clerkPromise ?? loadClerk();
}

/**
 * Mint a fresh short-lived session JWT for API/WebSocket auth. Returns `null` if
 * there is no active Clerk session (e.g. anonymous) or outside the cloud build.
 * Clerk tokens are short-lived (~60s) and minted per use — never cached.
 */
export async function getClerkToken(): Promise<string | null> {
  const clerk = await getClerk();
  if (!clerk?.session) return null;
  try {
    return await clerk.session.getToken();
  } catch (err) {
    console.error('[clerk] getToken failed:', err);
    return null;
  }
}

/** True if a Clerk session is currently active. */
export async function hasClerkSession(): Promise<boolean> {
  const clerk = await getClerk();
  return !!clerk?.session;
}

/** Return the active Clerk user object (untyped), or `null`. */
export async function getClerkUser(): Promise<unknown | null> {
  const clerk = await getClerk();
  return clerk?.user ?? null;
}

/** Return the active organization `{ id, name, slug }`, or `null` (personal workspace). */
export async function getClerkOrganization(): Promise<{
  id: string;
  name?: string;
  slug?: string;
} | null> {
  const clerk = await getClerk();
  return clerk?.organization ?? null;
}

/** Sign the user out of Clerk. No-op outside the cloud build. */
export async function clerkSignOut(): Promise<void> {
  const clerk = await getClerk();
  if (!clerk) return;
  try {
    await clerk.signOut();
  } catch (err) {
    console.error('[clerk] signOut failed:', err);
  }
}

/**
 * Subscribe to Clerk auth-state changes (session/user/org). Returns an
 * unsubscribe function, or a no-op if Clerk is unavailable.
 */
export async function onClerkChange(cb: (payload: unknown) => void): Promise<() => void> {
  const clerk = await getClerk();
  if (!clerk) return () => {};
  return clerk.addListener(cb);
}

// --- Prebuilt-component mounting helpers -----------------------------------
// Each returns a cleanup function that unmounts the component (use it from
// onMount's return / onDestroy in the host component).

export async function mountSignIn(
  node: HTMLElement,
  props: Record<string, unknown> = {}
): Promise<() => void> {
  const clerk = await getClerk();
  if (!clerk) return () => {};
  clerk.mountSignIn(node, props);
  return () => clerk.unmountSignIn(node);
}

export async function mountSignUp(
  node: HTMLElement,
  props: Record<string, unknown> = {}
): Promise<() => void> {
  const clerk = await getClerk();
  if (!clerk) return () => {};
  clerk.mountSignUp(node, props);
  return () => clerk.unmountSignUp(node);
}

export async function mountUserProfile(
  node: HTMLElement,
  props: Record<string, unknown> = {}
): Promise<() => void> {
  const clerk = await getClerk();
  if (!clerk) return () => {};
  clerk.mountUserProfile(node, props);
  return () => clerk.unmountUserProfile(node);
}

export async function mountOrganizationSwitcher(
  node: HTMLElement,
  props: Record<string, unknown> = {}
): Promise<() => void> {
  const clerk = await getClerk();
  if (!clerk) return () => {};
  clerk.mountOrganizationSwitcher(node, props);
  return () => clerk.unmountOrganizationSwitcher(node);
}

/** Open Clerk's prebuilt account/security modal (cloud only). No-op otherwise. */
export async function openUserProfile(props: Record<string, unknown> = {}): Promise<void> {
  const clerk = await getClerk();
  if (!clerk) return;
  clerk.openUserProfile(props);
}
