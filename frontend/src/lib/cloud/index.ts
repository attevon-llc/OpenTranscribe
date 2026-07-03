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
 *  - This stub defines the seam contract: the managed edition must export the
 *    same symbols with compatible signatures. Change here first, mirror there.
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
