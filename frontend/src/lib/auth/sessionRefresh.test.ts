import { describe, expect, it } from 'vitest';
import {
  createSessionRefresher,
  type ChannelLike,
  type LockLike,
  type RefreshDeps,
} from './sessionRefresh';

/** A browser-wide FIFO lock, like `navigator.locks` shared by every tab. */
function sharedLock(): LockLike {
  let tail: Promise<unknown> = Promise.resolve();
  let pending = 0; // granted or queued requests: the lock is not "available" while > 0
  const run = <T>(callback: () => Promise<T>): Promise<T> => {
    pending += 1;
    const result = tail.then(callback, callback).finally(() => {
      pending -= 1;
    });
    tail = result.catch(() => undefined);
    return result;
  };
  return {
    run,
    tryRun: <T>(callback: () => Promise<T>): Promise<T | false> =>
      pending > 0 ? Promise.resolve(false as const) : run(callback),
  };
}

/** A BroadcastChannel hub: a message reaches every OTHER tab, asynchronously. */
function channelHub() {
  const tabs: Array<(event: { data: unknown }) => void> = [];
  return (): ChannelLike => {
    let own: ((event: { data: unknown }) => void) | null = null;
    return {
      postMessage(data: unknown) {
        for (const listener of tabs) {
          if (listener !== own) setTimeout(() => listener({ data }), 0);
        }
      },
      addEventListener(_type, listener) {
        own = listener;
        tabs.push(listener);
      },
    };
  };
}

/** The server: one refresh cookie, valid until a refresh rotates it. */
function fakeServer() {
  const state = { generation: 0, accessValid: false, refreshes: 0, concurrent: 0, peak: 0 };
  const deps = (): Pick<RefreshDeps, 'refresh' | 'sessionIsValid'> => ({
    refresh: async () => {
      state.concurrent += 1;
      state.peak = Math.max(state.peak, state.concurrent);
      await new Promise((r) => setTimeout(r, 20));
      state.refreshes += 1;
      state.generation += 1;
      state.accessValid = true;
      state.concurrent -= 1;
    },
    sessionIsValid: async () => state.accessValid,
  });
  return { state, deps };
}

describe('createSessionRefresher', () => {
  it('two tabs sharing a lock refresh once; the second skips', async () => {
    const server = fakeServer();
    const lock = sharedLock();
    const hub = channelHub();
    const tabA = createSessionRefresher({ ...server.deps(), locks: lock, channel: hub() });
    const tabB = createSessionRefresher({ ...server.deps(), locks: lock, channel: hub() });

    const outcomes = await Promise.all([tabA(), tabB()]);

    expect(outcomes).toEqual(['refreshed', 'skipped']);
    expect(server.state.refreshes).toBe(1);
  });

  it('never runs two refreshes at once across tabs, even when neither can skip', async () => {
    const server = fakeServer();
    const lock = sharedLock();
    const neverValid = { ...server.deps(), sessionIsValid: async () => false };
    const tabA = createSessionRefresher({ ...neverValid, locks: lock });
    const tabB = createSessionRefresher({ ...neverValid, locks: lock });

    await Promise.all([tabA(), tabB()]);

    expect(server.state.refreshes).toBe(2);
    expect(server.state.peak).toBe(1);
  });

  it('a broadcast that arrives while waiting skips without re-probing the session', async () => {
    const server = fakeServer();
    const lock = sharedLock();
    const hub = channelHub();
    let probes = 0;
    const probed = {
      ...server.deps(),
      sessionIsValid: async () => {
        probes += 1;
        return false;
      },
    };
    const tabA = createSessionRefresher({ ...probed, locks: lock, channel: hub() });
    const tabB = createSessionRefresher({
      ...probed,
      // Tab B is granted the lock only after tab A's broadcast has been delivered.
      locks: {
        tryRun: (callback) => lock.tryRun(callback),
        run: (callback) => lock.run(() => new Promise((r) => setTimeout(r, 10)).then(callback)),
      },
      channel: hub(),
    });

    const outcomes = await Promise.all([tabA(), tabB()]);

    expect(outcomes).toEqual(['refreshed', 'skipped']);
    expect(probes).toBe(0);
  });

  it('a tab that gets the lock at once refreshes without probing the session', async () => {
    const server = fakeServer();
    let probes = 0;
    const tab = createSessionRefresher({
      ...server.deps(),
      sessionIsValid: async () => {
        probes += 1;
        return true;
      },
      locks: sharedLock(),
    });

    expect(await tab()).toBe('refreshed');
    expect(probes).toBe(0);
    expect(server.state.refreshes).toBe(1);
  });

  it('concurrent callers in one tab share one refresh', async () => {
    const server = fakeServer();
    const tab = createSessionRefresher({ ...server.deps(), locks: sharedLock() });

    const outcomes = await Promise.all([tab(), tab(), tab()]);

    expect(outcomes).toEqual(['refreshed', 'refreshed', 'refreshed']);
    expect(server.state.refreshes).toBe(1);
  });

  it('without Web Locks, waits a random delay and skips when another tab refreshed', async () => {
    const server = fakeServer();
    const hub = channelHub();
    const delays: number[] = [];
    const sleep = async (ms: number) => {
      delays.push(ms);
      await new Promise((r) => setTimeout(r, ms));
    };
    const tabA = createSessionRefresher({
      ...server.deps(),
      locks: null,
      channel: hub(),
      random: () => 0,
      sleep,
    });
    const tabB = createSessionRefresher({
      ...server.deps(),
      locks: null,
      channel: hub(),
      random: () => 0.99,
      sleep,
    });

    const outcomes = await Promise.all([tabA(), tabB()]);

    expect(outcomes).toEqual(['refreshed', 'skipped']);
    expect(server.state.refreshes).toBe(1);
    expect(delays).toEqual([0, 247]);
  });

  it('a refused refresh rejects every caller and the next call tries again', async () => {
    let attempts = 0;
    const tab = createSessionRefresher({
      refresh: async () => {
        attempts += 1;
        throw new Error('401');
      },
      sessionIsValid: async () => false,
      locks: sharedLock(),
    });

    await expect(Promise.all([tab(), tab()])).rejects.toThrow('401');
    await expect(tab()).rejects.toThrow('401');
    expect(attempts).toBe(2);
  });
});
