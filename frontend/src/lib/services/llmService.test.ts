/**
 * `llmService` is a TTL-cached wrapper around `GET /llm/status` (60 s while
 * available, 10 s after a failure) with a shared in-flight-request guard
 * (BC-32): concurrent callers hitting a cold/expired cache must await the
 * SAME `axiosInstance.get` rather than each firing their own — otherwise,
 * since promise resolution order is not guaranteed to match request-issue
 * order, an earlier request resolving LAST would overwrite a newer cached
 * value with stale data. `isAvailable()` has zero production callers today
 * but is exported, so this pins the dedup for whoever calls it next.
 */
import { describe, it, expect, vi, beforeEach } from 'vitest';

const mockAxiosInstance = vi.hoisted(() => ({ get: vi.fn(), post: vi.fn() }));
vi.mock('$lib/axios', () => ({ default: mockAxiosInstance }));

vi.mock('$stores/locale', () => ({
  t: {
    subscribe: (run: (value: (key: string, vars?: Record<string, unknown>) => string) => void) => {
      run((key: string) => key);
      return () => {};
    },
  },
}));

import { llmService } from './llmService';

const STATUS_OK = {
  available: true,
  user_id: 'user-1',
  provider: 'openai',
  model: 'gpt-4o',
  message: 'ready',
};

const STATUS_STALE = {
  available: true,
  user_id: 'user-1',
  provider: 'openai',
  model: 'gpt-3.5-turbo',
  message: 'stale',
};

beforeEach(() => {
  vi.clearAllMocks();
  llmService.clearCache();
});

describe('getStatus', () => {
  it('fetches once and caches for the TTL window — a second call within it does not refetch', async () => {
    mockAxiosInstance.get.mockResolvedValue({ data: STATUS_OK });

    const first = await llmService.getStatus();
    const second = await llmService.getStatus();

    expect(mockAxiosInstance.get).toHaveBeenCalledTimes(1);
    expect(first).toEqual(STATUS_OK);
    expect(second).toEqual(STATUS_OK);
  });

  it('refetches once the 60s "available" cache window has elapsed', async () => {
    vi.useFakeTimers();
    try {
      mockAxiosInstance.get.mockResolvedValueOnce({ data: STATUS_OK });
      await llmService.getStatus();
      expect(mockAxiosInstance.get).toHaveBeenCalledTimes(1);

      vi.advanceTimersByTime(60_001);

      mockAxiosInstance.get.mockResolvedValueOnce({ data: STATUS_STALE });
      const refetched = await llmService.getStatus();
      expect(mockAxiosInstance.get).toHaveBeenCalledTimes(2);
      expect(refetched).toEqual(STATUS_STALE);
    } finally {
      vi.useRealTimers();
    }
  });

  it('uses the shorter 10s cache window after a failure, not the 60s "available" window', async () => {
    vi.useFakeTimers();
    try {
      mockAxiosInstance.get.mockRejectedValueOnce(new Error('network down'));
      const failed = await llmService.getStatus();
      expect(failed.available).toBe(false);
      expect(mockAxiosInstance.get).toHaveBeenCalledTimes(1);

      // Still within the 10s failure window — must serve the cached failure.
      vi.advanceTimersByTime(9_000);
      await llmService.getStatus();
      expect(mockAxiosInstance.get).toHaveBeenCalledTimes(1);

      // Past the 10s (but well under the 60s) window — must refetch.
      vi.advanceTimersByTime(1_001);
      mockAxiosInstance.get.mockResolvedValueOnce({ data: STATUS_OK });
      await llmService.getStatus();
      expect(mockAxiosInstance.get).toHaveBeenCalledTimes(2);
    } finally {
      vi.useRealTimers();
    }
  });

  it('forceRefresh bypasses a still-valid cache and refetches', async () => {
    mockAxiosInstance.get.mockResolvedValueOnce({ data: STATUS_OK });
    await llmService.getStatus();
    expect(mockAxiosInstance.get).toHaveBeenCalledTimes(1);

    mockAxiosInstance.get.mockResolvedValueOnce({ data: STATUS_STALE });
    const forced = await llmService.getStatus(true);
    expect(mockAxiosInstance.get).toHaveBeenCalledTimes(2);
    expect(forced).toEqual(STATUS_STALE);
  });

  it('returns a default unavailable status on request failure, without throwing', async () => {
    mockAxiosInstance.get.mockRejectedValue(new Error('boom'));

    const status = await llmService.getStatus();

    expect(status.available).toBe(false);
    expect(status.provider).toBeNull();
    expect(status.model).toBeNull();
    expect(typeof status.message).toBe('string');
  });

  it('BC-32: deduplicates concurrent calls on a cold cache into a single in-flight request', async () => {
    let resolveGet!: (v: { data: typeof STATUS_OK }) => void;
    mockAxiosInstance.get.mockReturnValue(new Promise((r) => (resolveGet = r)));

    const first = llmService.getStatus();
    const second = llmService.getStatus();
    resolveGet({ data: STATUS_OK });

    const [firstResult, secondResult] = await Promise.all([first, second]);

    expect(mockAxiosInstance.get).toHaveBeenCalledTimes(1);
    // Both concurrent callers must observe the SAME resolved status.
    expect(firstResult).toEqual(STATUS_OK);
    expect(secondResult).toEqual(STATUS_OK);
  });

  it('forceRefresh does NOT join an in-flight non-forced fetch — it always issues its own request', async () => {
    let resolveInFlight!: (v: { data: typeof STATUS_STALE }) => void;
    mockAxiosInstance.get.mockReturnValueOnce(new Promise((r) => (resolveInFlight = r)));

    const backgroundCall = llmService.getStatus(); // cold cache, starts the shared in-flight fetch

    mockAxiosInstance.get.mockResolvedValueOnce({ data: STATUS_OK });
    const forced = await llmService.getStatus(true);

    expect(mockAxiosInstance.get).toHaveBeenCalledTimes(2);
    expect(forced).toEqual(STATUS_OK);

    resolveInFlight({ data: STATUS_STALE });
    await backgroundCall;
  });

  it('clearCache() bumps the generation so a stale in-flight resolution does not clobber a subsequent forced refetch', async () => {
    let resolveStale!: (v: { data: typeof STATUS_STALE }) => void;
    mockAxiosInstance.get.mockReturnValueOnce(new Promise((r) => (resolveStale = r)));

    const staleInFlight = llmService.getStatus(); // starts a non-forced fetch, cache still cold

    llmService.clearCache(); // e.g. settings changed mid-request

    mockAxiosInstance.get.mockResolvedValueOnce({ data: STATUS_OK });
    const fresh = await llmService.getStatus(true);
    expect(fresh).toEqual(STATUS_OK);

    // The pre-clear fetch resolves AFTER the forced refetch already completed.
    resolveStale({ data: STATUS_STALE });
    await staleInFlight;

    // Its result must have been discarded — the cache still reflects the fresh forced fetch.
    const cached = await llmService.getStatus();
    expect(cached).toEqual(STATUS_OK);
    expect(mockAxiosInstance.get).toHaveBeenCalledTimes(2);
  });
});

describe('isAvailable', () => {
  it('passes through the `available` field from getStatus', async () => {
    mockAxiosInstance.get.mockResolvedValue({ data: STATUS_OK });
    expect(await llmService.isAvailable()).toBe(true);
  });

  it('returns false when the underlying status is unavailable', async () => {
    mockAxiosInstance.get.mockResolvedValue({
      data: { available: false, user_id: 'user-1', provider: null, model: null, message: 'off' },
    });
    expect(await llmService.isAvailable()).toBe(false);
  });

  it('returns false (not throw) when the request fails', async () => {
    mockAxiosInstance.get.mockRejectedValue(new Error('down'));
    expect(await llmService.isAvailable()).toBe(false);
  });
});

describe('getProviders', () => {
  it('returns the provider list on success', async () => {
    const data = { providers: ['openai', 'anthropic'], total: 2, message: 'ok' };
    mockAxiosInstance.get.mockResolvedValue({ data });

    expect(await llmService.getProviders()).toEqual(data);
    expect(mockAxiosInstance.get).toHaveBeenCalledWith('/llm/providers');
  });

  it('throws with a resolved error message on failure', async () => {
    mockAxiosInstance.get.mockRejectedValue(new Error('unreachable'));

    await expect(llmService.getProviders()).rejects.toThrow('unreachable');
  });
});

describe('testConnection', () => {
  it('returns the connection test result on success', async () => {
    const data = { success: true, message: 'connected', provider: 'openai', model: 'gpt-4o' };
    mockAxiosInstance.post.mockResolvedValue({ data });

    expect(await llmService.testConnection()).toEqual(data);
    expect(mockAxiosInstance.post).toHaveBeenCalledWith('/llm/test-connection');
  });

  it('returns a failure result (not throw) when the request fails', async () => {
    mockAxiosInstance.post.mockRejectedValue(new Error('refused'));

    const result = await llmService.testConnection();

    expect(result.success).toBe(false);
    expect(typeof result.message).toBe('string');
  });
});
