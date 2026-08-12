/**
 * Edition capabilities / entitlements store.
 *
 * Mirrors GET /api/system/capabilities — the backend decides which feature
 * surfaces exist for this deployment (community: everything; cloud: the
 * managed subset, tier-aware). The UI renders only enabled surfaces; the
 * backend independently 404s disabled endpoints, so this is purely cosmetic
 * gating on top of a real server-side gate.
 *
 * Fail-open by design: until the endpoint responds (or if it ever fails),
 * the store holds community defaults so a self-hosted instance — or a blip
 * during startup — never hides features.
 */

import { writable } from 'svelte/store';
import axiosInstance from '$lib/axios';

type CapabilityAudience = 'user' | 'team' | 'org_admin' | 'platform';

export interface CapabilitiesState {
  edition: 'community' | 'cloud';
  loaded: boolean;
  capabilities: Record<string, boolean>;
  audience: Record<string, CapabilityAudience>;
}

const COMMUNITY_DEFAULTS: CapabilitiesState = {
  edition: 'community',
  loaded: false,
  capabilities: {},
  audience: {},
};

export const capabilities = writable<CapabilitiesState>(COMMUNITY_DEFAULTS);

/**
 * True unless the backend explicitly disabled the key. Unknown keys default
 * to enabled (fail-open) — the cloud resolver explicitly sets false for
 * every surface it hides.
 *
 * This is the OPPOSITE of the backend's `capability_enabled`, which returns
 * false for an unknown key (fail-closed). The asymmetry is intentional (a
 * blank/failed capabilities fetch must not blank the self-hosted UI) but it
 * hides drift: a `cap:` key the backend has never declared renders here
 * forever and vanishes the moment someone declares it as `False`. Hence
 * `backend/tests/unit/test_capability_contract.py`, which pins every `cap:`
 * string in SettingsModal.svelte to a declared backend capability.
 */
export function isCapabilityEnabled(state: CapabilitiesState, key: string): boolean {
  return state.capabilities[key] !== false;
}

/**
 * Drop the fetched capability map back to community defaults (`loaded: false`).
 *
 * Registered in `$lib/session/clearUserState`. Without it, the cloud edition
 * leaked one user's TIER-SCOPED capability map into the next session on the same
 * tab: `loadCapabilities()` has exactly one call site (`routes/+layout.svelte`
 * `onMount`), and an SPA login does not re-run `onMount`, so User B saw User A's
 * enabled surfaces until a hard reload.
 *
 * Resetting to `loaded: false` (not `loaded: true`) is deliberate: consumers that
 * wait for `loaded` must wait for the NEXT user's fetch, not read the fail-open
 * empty map as an authoritative answer.
 */
export function resetCapabilities(): void {
  capabilities.set({ ...COMMUNITY_DEFAULTS, capabilities: {}, audience: {} });
}

/** Fetch once at app bootstrap; safe to call again (e.g. after login). */
export async function loadCapabilities(): Promise<void> {
  try {
    const response = await axiosInstance.get('/system/capabilities');
    capabilities.set({
      edition: response.data?.edition === 'cloud' ? 'cloud' : 'community',
      loaded: true,
      capabilities: response.data?.capabilities ?? {},
      audience: response.data?.audience ?? {},
    });
  } catch {
    // Fail-open: keep community defaults (everything visible).
    capabilities.set({ ...COMMUNITY_DEFAULTS, loaded: true });
  }
}
