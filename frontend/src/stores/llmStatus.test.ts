/**
 * `llmStatusStore` centralizes LLM availability polling. The riskiest behaviors
 * are the ones that are easy to get subtly wrong: concurrent `initialize()`
 * calls must share one in-flight request rather than double-firing, a failed
 * initialize must be retryable rather than permanently wedging the store, and
 * a failed refresh must overwrite stale "available" state with an honest
 * failure status rather than silently leaving the old value on screen.
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

describe('handleNotification', () => {
  it('refreshes on llm_settings_changed / llm_status_changed, ignores everything else', async () => {
    mockGetStatus.mockResolvedValue(AVAILABLE_STATUS);
    expect(get(llmStatusStore).available).toBe(false); // baseline: nothing fetched yet

    llmStatusStore.handleNotification('some_other_event', {});
    expect(mockGetStatus).not.toHaveBeenCalled();
    expect(get(llmStatusStore).available).toBe(false);

    llmStatusStore.handleNotification('llm_settings_changed', {});
    await vi.waitFor(() => expect(get(llmStatusStore).available).toBe(true));
    expect(mockGetStatus).toHaveBeenCalledTimes(1);

    llmStatusStore.reset();
    mockGetStatus.mockClear();
    mockGetStatus.mockResolvedValue(AVAILABLE_STATUS);

    llmStatusStore.handleNotification('llm_status_changed', {});
    await vi.waitFor(() => expect(get(llmStatusStore).available).toBe(true));
    expect(mockGetStatus).toHaveBeenCalledTimes(1);
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
