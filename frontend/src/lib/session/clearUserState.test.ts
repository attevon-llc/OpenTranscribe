/**
 * Behavioural tests for `$lib/session/clearUserState`.
 *
 * DEFECT THESE CATCH: this module had 0/24 line coverage — no test ever invoked
 * it — which is exactly why `apiCache.clear()` could be absent from the "single
 * source of truth for session cleanup" without anyone noticing. The companion
 * `clearUserState.completeness.test.ts` checks that every stateful module is
 * REGISTERED; these check that a real call actually clears what it claims to and
 * preserves what it claims to.
 *
 * These run the real module against the real stores (no mocks) — cleanup is
 * `Promise.allSettled` and best-effort by design, so a mocked store would let a
 * genuinely-throwing cleanup pass.
 */

import { describe, it, expect, beforeEach, vi } from 'vitest';
import { get } from 'svelte/store';
import { clearUserState } from '$lib/session/clearUserState';
import { apiCache, CacheTTL } from '$lib/apiCache';
import { capabilities } from '$stores/capabilities';

describe('clearUserState', () => {
  beforeEach(() => {
    localStorage.clear();
    apiCache.clear();
  });

  it('empties apiCache, so User B cannot read User A’s cached file list', async () => {
    // apiCache keys are NOT user-scoped ('tags:all', 'files:page:1:...'), and an
    // SPA login does not reload the module holding the Map. Before the
    // registration existed, apiCache.clear() had zero call sites app-wide.
    await apiCache.getOrFetch('tags:all', async () => ['user-a-tag'], CacheTTL.FILES);
    await apiCache.getOrFetch('files:page:1:', async () => ['user-a-file'], CacheTTL.FILES);
    expect(apiCache.stats().size).toBe(2);

    await clearUserState();

    expect(apiCache.stats().size).toBe(0);
  });

  it('resets capabilities to unloaded community defaults (cloud tier leak)', async () => {
    capabilities.set({
      edition: 'cloud',
      loaded: true,
      capabilities: { 'cap:transcription.diarization': false, 'cap:admin.platform': true },
      audience: { 'cap:admin.platform': 'platform' },
      maxUploadBytes: 5_000_000,
    });

    await clearUserState();

    const state = get(capabilities);
    expect(state.capabilities).toEqual({});
    expect(state.audience).toEqual({});
    expect(state.edition).toBe('community');
    // `loaded: false` matters: a consumer that gates on `loaded` must wait for the
    // NEXT user's fetch rather than treat the empty fail-open map as an answer.
    expect(state.loaded).toBe(false);
    // Same tier-scoped leak as above, for the upload ceiling: `undefined` (not the
    // stale 5,000,000) makes $lib/utils/uploadLimits fall back to its coded default
    // until the next user's fetch resolves.
    expect(state.maxUploadBytes).toBeUndefined();
  });

  it('removes the localStorage keys that hold user data', async () => {
    localStorage.setItem('notifications', JSON.stringify([{ id: '1', title: 'User A file done' }]));
    localStorage.setItem('upload_queue', JSON.stringify([{ id: 'u1' }]));
    localStorage.setItem('opentr:uploadPreviousValues', JSON.stringify({ collection: 'Secret' }));

    await clearUserState();

    expect(localStorage.getItem('notifications')).toBeNull();
    expect(localStorage.getItem('upload_queue')).toBeNull();
    expect(localStorage.getItem('opentr:uploadPreviousValues')).toBeNull();
  });

  it('preserves UI preferences — over-clearing is a bug too', async () => {
    // A logout that resets the user's theme and language is a regression, not
    // extra safety. These are explicitly documented as preserved.
    localStorage.setItem('theme', 'dark');
    localStorage.setItem('i18nextLng', 'fr');
    localStorage.setItem('galleryViewMode', 'list');
    localStorage.setItem('uploadManagerPosition', 'bottom-left');

    await clearUserState();

    expect(localStorage.getItem('theme')).toBe('dark');
    expect(localStorage.getItem('i18nextLng')).toBe('fr');
    expect(localStorage.getItem('galleryViewMode')).toBe('list');
    expect(localStorage.getItem('uploadManagerPosition')).toBe('bottom-left');
  });

  it('resolves even when localStorage throws (private browsing / quota)', async () => {
    // The per-key try/catch is load-bearing: a throwing localStorage must not
    // leave the user half-logged-out with a rejected promise.
    //
    // The hook goes on Storage.prototype deliberately. jsdom's `localStorage` is
    // a Proxy whose `set` writes a storage ITEM, so `localStorage.removeItem = fn`
    // stores a key named "removeItem" and never intercepts the call — this test
    // passed for exactly that reason before, proving nothing.
    const hook = vi.spyOn(Storage.prototype, 'removeItem').mockImplementation(() => {
      throw new Error('QuotaExceededError (private browsing)');
    });
    try {
      await expect(clearUserState()).resolves.toBeUndefined();
      expect(hook).toHaveBeenCalledWith('notifications');
    } finally {
      hook.mockRestore();
    }
  });
});
