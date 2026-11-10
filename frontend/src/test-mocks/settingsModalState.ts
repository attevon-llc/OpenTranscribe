/**
 * Per-test reset for the `SettingsModal.*` suites. Kept apart from `settingsModalMocks.ts`: that
 * module is imported from inside `vi.mock` factories for `$stores/auth`, and importing the real
 * stores from there would wait on the very mock being built and hang the run.
 */
import { vi } from 'vitest';
import { capabilities } from '$stores/capabilities';
import { settingsModalStore } from '$stores/settingsModalStore';
import { resetAppStores } from '$testMocks/app-state.svelte';

/** Fresh app stores, a closed modal, and loaded capabilities for `edition` with nothing gated. */
export const resetSettingsModalState = (edition: 'cloud' | 'community' = 'community') => {
  vi.clearAllMocks();
  resetAppStores();
  settingsModalStore.reset();
  capabilities.set({
    edition,
    loaded: true,
    capabilities: {},
    audience: {},
    maxUploadBytes: undefined,
  });
};
