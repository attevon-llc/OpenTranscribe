/**
 * `llmStatusStore` centralizes LLM availability polling. The riskiest behaviors
 * are the ones that are easy to get subtly wrong: concurrent `initialize()`
 * calls must share one in-flight request rather than double-firing, a failed
 * initialize must be retryable rather than permanently wedging the store, and
 * a failed refresh must overwrite stale "available" state with an honest
 * failure status rather than silently leaving the old value on screen. And a
 * settings change must reach every gated button without a page reload: that is
 * `invalidate()` plus the focus/visibility re-check.
 */
import { describe, it, expect, vi, beforeEach, afterEach } from 'vitest';
import { get } from 'svelte/store';

const mockGetStatus = vi.hoisted(() => vi.fn());
vi.mock('$lib/services/llmService', () => ({
  llmService: { getStatus: mockGetStatus },
}));

import { llmStatusStore } from './llmStatus';

const AVAILABLE_STATUS = {
  available: true,
  user_id: '1',
  provider: 'openai',
  model: 'gpt-4',
  message: 'ok',
};

beforeEach(() => {
  vi.clearAllMocks();
  llmStatusStore.reset();
});

afterEach(() => {
  llmStatusStore.reset();
  vi.useRealTimers();
});

describe('initialize', () => {
  it('sets checking, then resolves with the fetched status', async () => {
    mockGetStatus.mockResolvedValue(AVAILABLE_STATUS);

    const promise = llmStatusStore.initialize();
    expect(get(llmStatusStore).checking).toBe(true);
    await promise;

    const state = get(llmStatusStore);
    expect(state.checking).toBe(false);
    expect(state.available).toBe(true);
    expect(state.status).toEqual(AVAILABLE_STATUS);
    expect(state.lastChecked).toBeInstanceOf(Date);
  });

  it('deduplicates concurrent calls into one in-flight request', async () => {
    let resolveStatus!: (v: typeof AVAILABLE_STATUS) => void;
    mockGetStatus.mockReturnValue(new Promise((r) => (resolveStatus = r)));

    const first = llmStatusStore.initialize();
    const second = llmStatusStore.initialize();

    resolveStatus(AVAILABLE_STATUS);
    await first;
    await second;

    expect(mockGetStatus).toHaveBeenCalledTimes(1);
    // Both callers must observe the SAME resolved state, not a stale one from
    // whichever call didn't actually reach the network.
    expect(get(llmStatusStore).available).toBe(true);
    expect(get(llmStatusStore).checking).toBe(false);
  });

  it('suppresses ALL later calls after a success, not just genuinely-concurrent ones', async () => {
    // `initPromise` (the dedup flag) is only cleared on the failure path and in
    // `reset()` — never after a success. So a second call made well after the first
    // has already resolved still short-circuits to the cached promise instead of
    // issuing a fresh request. This pins that actual behavior explicitly (other tests
    // in this file all call `reset()` in `beforeEach`, which would mask it). Callers
    // (`+layout.svelte`'s reactive auth block, its `onMount` guard, and
    // `SelectiveReprocessModal.svelte`) rely on exactly this: `initialize()` as a
    // cheap idempotent "ensure ready" call, with `startMonitoring()`'s 2-minute
    // interval responsible for keeping state fresh afterward.
    mockGetStatus.mockResolvedValue(AVAILABLE_STATUS);

    await llmStatusStore.initialize();
    expect(mockGetStatus).toHaveBeenCalledTimes(1);
    expect(get(llmStatusStore).available).toBe(true);

    await llmStatusStore.initialize();
    expect(mockGetStatus).toHaveBeenCalledTimes(1);
    expect(get(llmStatusStore).available).toBe(true);
    expect(get(llmStatusStore).checking).toBe(false);
  });

  it('allows a retry after a failed initialize instead of wedging permanently', async () => {
    mockGetStatus.mockRejectedValueOnce(new Error('network down'));
    await llmStatusStore.initialize();
    expect(get(llmStatusStore).available).toBe(false);

    mockGetStatus.mockResolvedValueOnce(AVAILABLE_STATUS);
    await llmStatusStore.initialize();

    expect(get(llmStatusStore).available).toBe(true);
    expect(mockGetStatus).toHaveBeenCalledTimes(2);
  });
});

describe('refreshStatus', () => {
  it('overwrites stale "available" state with an honest failure on error', async () => {
    mockGetStatus.mockResolvedValueOnce(AVAILABLE_STATUS);
    await llmStatusStore.initialize();
    expect(get(llmStatusStore).available).toBe(true);

    mockGetStatus.mockRejectedValueOnce(new Error('provider unreachable'));
    await llmStatusStore.refreshStatus();

    const state = get(llmStatusStore);
    expect(state.available).toBe(false);
    expect(state.checking).toBe(false);
    expect(state.status?.available).toBe(false);
  });
});

describe('monitoring', () => {
  it('polls on the interval, but skips a tick if a check is already in flight', async () => {
    vi.useFakeTimers();
    mockGetStatus.mockResolvedValue(AVAILABLE_STATUS);
    await llmStatusStore.initialize(); // also calls startMonitoring()

    let resolveSecond!: (v: typeof AVAILABLE_STATUS) => void;
    mockGetStatus.mockReturnValueOnce(new Promise((r) => (resolveSecond = r)));

    await vi.advanceTimersByTimeAsync(120000); // first poll tick — now "checking"
    expect(mockGetStatus).toHaveBeenCalledTimes(2); // initialize + first poll
    expect(get(llmStatusStore).checking).toBe(true); // the in-flight poll is real store state

    await vi.advanceTimersByTimeAsync(120000); // second tick lands while still checking
    expect(mockGetStatus).toHaveBeenCalledTimes(2); // must NOT have fired again
    expect(get(llmStatusStore).checking).toBe(true); // still the SAME in-flight check

    resolveSecond(AVAILABLE_STATUS);
    llmStatusStore.stopMonitoring();
  });

  it('stopMonitoring prevents any further polls', async () => {
    vi.useFakeTimers();
    mockGetStatus.mockResolvedValue(AVAILABLE_STATUS);
    await llmStatusStore.initialize();
    const lastCheckedBeforeStop = get(llmStatusStore).lastChecked;
    mockGetStatus.mockClear();

    llmStatusStore.stopMonitoring();
    await vi.advanceTimersByTimeAsync(300000);

    expect(mockGetStatus).not.toHaveBeenCalled();
    // If a poll had fired, lastChecked would have moved forward.
    expect(get(llmStatusStore).lastChecked).toEqual(lastCheckedBeforeStop);
  });
});

describe('invalidate (the one call every LLM settings flow makes)', () => {
  it('re-fetches at once, even after a successful initialize made initialize() a no-op', async () => {
    mockGetStatus.mockResolvedValueOnce({ ...AVAILABLE_STATUS, available: false });
    await llmStatusStore.initialize();
    expect(get(llmStatusStore).available).toBe(false);

    mockGetStatus.mockResolvedValueOnce(AVAILABLE_STATUS);
    await llmStatusStore.invalidate();

    expect(mockGetStatus).toHaveBeenCalledTimes(2);
    expect(get(llmStatusStore).available).toBe(true);
  });

  it('restarts the background re-check that a reset() had stopped', async () => {
    vi.useFakeTimers();
    mockGetStatus.mockResolvedValue(AVAILABLE_STATUS);
    await llmStatusStore.initialize();
    llmStatusStore.reset(); // what the settings panel used to do with no configuration

    mockGetStatus.mockResolvedValueOnce({ ...AVAILABLE_STATUS, available: false });
    await llmStatusStore.invalidate();
    expect(get(llmStatusStore).available).toBe(false);
    mockGetStatus.mockClear();
    await vi.advanceTimersByTimeAsync(120000);

    expect(mockGetStatus).toHaveBeenCalledTimes(1);
    expect(get(llmStatusStore).available).toBe(true); // the poll's answer landed
  });
});

describe('re-check when the page regains attention', () => {
  function setVisibility(state: 'visible' | 'hidden') {
    Object.defineProperty(document, 'visibilityState', { configurable: true, get: () => state });
    document.dispatchEvent(new Event('visibilitychange'));
  }

  afterEach(() => {
    // jsdom's own getter lives on the prototype; drop the instance override.
    delete (document as unknown as Record<string, unknown>).visibilityState;
  });

  it('re-checks when the tab becomes visible, not when it is hidden', async () => {
    vi.useFakeTimers();
    mockGetStatus.mockResolvedValueOnce({ ...AVAILABLE_STATUS, available: false });
    await llmStatusStore.initialize();
    mockGetStatus.mockResolvedValue(AVAILABLE_STATUS);
    await vi.advanceTimersByTimeAsync(60_000); // past the throttle window

    setVisibility('hidden');
    await vi.advanceTimersByTimeAsync(0);
    expect(mockGetStatus).toHaveBeenCalledTimes(1);

    setVisibility('visible');
    await vi.advanceTimersByTimeAsync(0);
    expect(mockGetStatus).toHaveBeenCalledTimes(2);
    expect(get(llmStatusStore).available).toBe(true);
  });

  it('re-checks on window focus, and a burst of focus events costs one request', async () => {
    vi.useFakeTimers();
    mockGetStatus.mockResolvedValueOnce({ ...AVAILABLE_STATUS, available: false });
    await llmStatusStore.initialize();
    mockGetStatus.mockResolvedValue(AVAILABLE_STATUS);
    await vi.advanceTimersByTimeAsync(60_000);
    mockGetStatus.mockClear();

    window.dispatchEvent(new Event('focus'));
    window.dispatchEvent(new Event('focus'));
    window.dispatchEvent(new Event('focus'));
    await vi.advanceTimersByTimeAsync(0);

    expect(mockGetStatus).toHaveBeenCalledTimes(1);
    expect(get(llmStatusStore).available).toBe(true);
  });

  it('does nothing after stopMonitoring (sign-out)', async () => {
    vi.useFakeTimers();
    mockGetStatus.mockResolvedValueOnce({ ...AVAILABLE_STATUS, available: false });
    await llmStatusStore.initialize();
    mockGetStatus.mockResolvedValue(AVAILABLE_STATUS);
    await vi.advanceTimersByTimeAsync(60_000);
    llmStatusStore.stopMonitoring();
    const lastCheckedBefore = get(llmStatusStore).lastChecked;
    mockGetStatus.mockClear();

    window.dispatchEvent(new Event('focus'));
    setVisibility('visible');
    await vi.advanceTimersByTimeAsync(0);

    expect(mockGetStatus).not.toHaveBeenCalled();
    expect(get(llmStatusStore).available).toBe(false);
    expect(get(llmStatusStore).lastChecked).toEqual(lastCheckedBefore);
  });
});

describe('reset', () => {
  it('restores initial state and stops monitoring', async () => {
    vi.useFakeTimers();
    mockGetStatus.mockResolvedValue(AVAILABLE_STATUS);
    await llmStatusStore.initialize();

    llmStatusStore.reset();

    expect(get(llmStatusStore)).toEqual({
      status: null,
      available: false,
      checking: false,
      lastChecked: null,
    });

    mockGetStatus.mockClear();
    await vi.advanceTimersByTimeAsync(300000);
    expect(mockGetStatus).not.toHaveBeenCalled();
  });
});
