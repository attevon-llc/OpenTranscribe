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

export type CapabilityAudience = 'user' | 'team' | 'org_admin' | 'platform';

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
 */
export function isCapabilityEnabled(state: CapabilitiesState, key: string): boolean {
  return state.capabilities[key] !== false;
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
