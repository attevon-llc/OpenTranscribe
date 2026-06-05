import { describe, it, expect, vi, beforeEach } from 'vitest';
import { get } from 'svelte/store';

vi.mock('$lib/axios', () => ({
  default: { get: vi.fn() },
}));

import axiosInstance from '$lib/axios';
import { capabilities, isCapabilityEnabled, loadCapabilities } from './capabilities';

const mockedGet = vi.mocked(axiosInstance.get);

describe('capabilities store', () => {
  beforeEach(() => {
    vi.clearAllMocks();
    capabilities.set({ edition: 'community', loaded: false, capabilities: {}, audience: {} });
  });

  it('defaults fail-open: unknown keys are enabled', () => {
    const state = get(capabilities);
    expect(isCapabilityEnabled(state, 'watch_sources')).toBe(true);
    expect(isCapabilityEnabled(state, 'anything.unknown')).toBe(true);
  });

  it('loads the cloud edition map and disables hidden surfaces', async () => {
    mockedGet.mockResolvedValueOnce({
      data: {
        edition: 'cloud',
        capabilities: { watch_sources: false, billing: true },
        audience: { billing: 'org_admin' },
      },
    });

    await loadCapabilities();
    const state = get(capabilities);

    expect(state.edition).toBe('cloud');
    expect(state.loaded).toBe(true);
    expect(isCapabilityEnabled(state, 'watch_sources')).toBe(false);
    expect(isCapabilityEnabled(state, 'billing')).toBe(true);
    // Keys the resolver didn't mention stay enabled (fail-open)
    expect(isCapabilityEnabled(state, 'recording')).toBe(true);
    expect(state.audience['billing']).toBe('org_admin');
  });

  it('falls back to community defaults when the endpoint fails', async () => {
    mockedGet.mockRejectedValueOnce(new Error('network down'));

    await loadCapabilities();
    const state = get(capabilities);

    expect(state.edition).toBe('community');
    expect(state.loaded).toBe(true);
    expect(isCapabilityEnabled(state, 'watch_sources')).toBe(true);
  });
});
