/**
 * Managed-edition seam module — community no-op stub.
 *
 * OpenTranscribe's frontend ships from one codebase in two editions (see
 * `$lib/edition`). The commercial managed edition layers proprietary auth and
 * billing UI onto the app by REPLACING this directory at image-build time with
 * its real implementation; the community/self-host build keeps these inert
 * stubs so shared files can import one stable surface without pulling any
 * external-vendor SDK into the bundle.
 *
 * Rules:
 *  - Shared files import ONLY `$lib/cloud` (this index) and the component
 *    paths under `$lib/cloud/components/` — never a submodule of the overlay.
 *  - Every call site must be gated by `isCloudEdition` (a compile-time
 *    constant), so all of this dead-code-eliminates out of community bundles.
 *  - Component seams: `UserMenuExtras.svelte` renders inside the user dropdown; overrides
 *    dispatch `select` when an entry is chosen so the menu closes.
 *  - This stub defines the seam contract: the managed edition must export the
 *    same symbols with compatible signatures. Change here first, mirror there.
 *    `seamContract.test.ts` pins the export list and the static importers.
 *
 * Bundle budget. `scripts/check-bundle-budget.mjs` gates the root layout chunk and its
 * static import closure, and `Navbar.svelte` imports this module statically, so THIS
 * FILE'S OWN STATIC IMPORT GRAPH IS PAID BY EVERY PAGE LOAD, however lazily other files
 * `import('$lib/cloud')`. Overlay rules:
 *  - Never write `export ... from './heavyModule'` for code that is only needed after
 *    sign-in or on demand. Export a thin wrapper that calls `import('./heavyModule')`
 *    inside the function body.
 *  - Core binds these statically, so they must stay cheap: `refreshUsage` and
 *    `refreshBilling` (Navbar), `usageStore`, `isOverLimit` and `showQuotaExceeded` (the
 *    upload surface; the only three core calls synchronously), and the components
 *    `UsageBadge`, `UpgradeButton` and `UserMenuExtras`. Every other symbol is reached
 *    only through a dynamic `import('$lib/cloud')`. A statically imported component
 *    brings whatever it imports with it.
 *  - `locales/*.json` packs are loaded eagerly (`$lib/i18n`) and count against the same
 *    budget.
 *  - If the derived build still exceeds the budget it sets the build-time variables
 *    `BUNDLE_BUDGET_ALLOWANCE_NODE_BYTES`, `BUNDLE_BUDGET_ALLOWANCE_CLOSURE_BYTES` and
 *    `BUNDLE_BUDGET_ALLOWANCE_REASON` (see `frontend/CLAUDE.md`, "Bundle budget gate").
 *    The allowance is capped, printed on every build, and a reason is mandatory.
 */

import { readable, type Readable } from 'svelte/store';

// --- External-auth surface (managed edition: hosted IdP; community: unused) --

/** Load the managed edition's auth SDK. Community stub: resolves `null`. */
export async function loadExternalAuth(): Promise<unknown | null> {
  return null;
}

/** True if a managed-edition auth session is active. Community stub: `false`. */
export async function hasExternalSession(): Promise<boolean> {
  return false;
}

/** Mint a short-lived bearer token for API/WS auth. Community stub: `null`. */
export async function getSessionToken(): Promise<string | null> {
  return null;
}

/** Sign out of the managed-edition session. Community stub: no-op. */
export async function externalSignOut(): Promise<void> {}

/**
 * When the current external session was authenticated, in epoch SECONDS (the
 * session's `auth_time`, not a refreshed token's `iat`). The idle guard uses it
 * for the absolute timeout and falls back to first-seen when this returns null.
 * Optional in an overlay: core resolves it at runtime. Community stub: `null`.
 */
export async function getExternalSessionAuthTime(): Promise<number | null> {
  return null;
}

/**
 * Start the provider's own re-authentication after the absolute session timeout.
 * Return `true` if the overlay took over (e.g. redirected to a hosted sign-in),
 * `false` to let core sign out via {@link externalSignOut} instead. Optional in an
 * overlay: core resolves it at runtime. Community stub: `false`.
 */
export async function externalReauthenticate(_reason: 'absolute' | 'idle'): Promise<boolean> {
  return false;
}

/** Subscribe to auth-state changes. Community stub: no-op unsubscriber. */
export async function onAuthChange(_cb: (payload: unknown) => void): Promise<() => void> {
  return () => {};
}

/** Mount the hosted sign-in component. Community stub: no-op cleanup. */
export async function mountSignIn(
  _node: HTMLElement,
  _props: Record<string, unknown> = {}
): Promise<() => void> {
  return () => {};
}

/** Open the hosted account/security portal. Community stub: no-op. */
export async function openAccountPortal(_props: Record<string, unknown> = {}): Promise<void> {}

// --- Billing / usage / quota surface (managed edition only) -----------------

/** Minimal usage snapshot shared files may read. The managed edition's store
 *  value is a structural superset of this. */
export interface UsageState {
  loaded: boolean;
}

/** Org usage for the current billing period. Community stub: never loads. */
export const usageStore: Readable<UsageState> = readable({ loaded: false });

/** True when usage exceeds the plan limit. Community stub: never. */
export function isOverLimit(_state: UsageState): boolean {
  return false;
}

/** Refresh usage from the billing API. Community stub: no-op. */
export async function refreshUsage(): Promise<void> {}

/** Refresh subscription state from the billing API. Community stub: no-op. */
export async function refreshBilling(): Promise<void> {}

/** Open the quota-exceeded modal. Community stub: no-op. */
export function showQuotaExceeded(_message = ''): void {}
