import { describe, it, expect, vi, beforeEach, afterEach } from 'vitest';

const start = vi.hoisted(() => vi.fn());
vi.mock('$stores/auth', () => ({ loginWithOIDC: start }));

import { startOidcLogin, OIDC_START_TIMEOUT_MS } from './oidcLogin';

describe('startOidcLogin', () => {
  beforeEach(() => vi.useFakeTimers());
  afterEach(() => vi.useRealTimers());

  it('returns the provider result when it answers in time', async () => {
    start.mockResolvedValue({ success: false, message: 'no' });
    await expect(startOidcLogin()).resolves.toEqual({ success: false, message: 'no' });
  });

  it('rejects with a timeout when the provider never answers', async () => {
    start.mockReturnValue(new Promise(() => {}));
    await Promise.all([
      expect(startOidcLogin()).rejects.toThrow(/^Connection timeout$/),
      vi.advanceTimersByTimeAsync(OIDC_START_TIMEOUT_MS),
    ]);
  });
});
