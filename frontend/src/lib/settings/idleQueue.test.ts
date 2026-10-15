import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';
import { runIdleQueue } from './idleQueue';

// A manual idle clock: callbacks run only when the test says the browser went idle.
let idleCallbacks: Map<number, () => void>;
let nextId: number;

function goIdle() {
  const [id, cb] = [...idleCallbacks.entries()][0] ?? [];
  if (id === undefined || !cb) return false;
  idleCallbacks.delete(id);
  cb();
  return true;
}

const flush = () => new Promise((resolve) => setTimeout(resolve, 0));

beforeEach(() => {
  idleCallbacks = new Map();
  nextId = 1;
  (window as any).requestIdleCallback = (cb: () => void) => {
    idleCallbacks.set(nextId, cb);
    return nextId++;
  };
  (window as any).cancelIdleCallback = (id: number) => idleCallbacks.delete(id);
});

afterEach(() => {
  delete (window as any).requestIdleCallback;
  delete (window as any).cancelIdleCallback;
});

describe('runIdleQueue', () => {
  it('starts nothing until the browser is idle', async () => {
    const task = vi.fn().mockResolvedValue(undefined);
    runIdleQueue([task]);
    await flush();
    expect(task).not.toHaveBeenCalled();
    goIdle();
    await flush();
    expect(task).toHaveBeenCalledTimes(1);
  });

  it('never has more than the concurrency cap in flight', async () => {
    const releases: Array<() => void> = [];
    let running = 0;
    let peak = 0;
    const tasks = Array.from({ length: 5 }, () => () => {
      running += 1;
      peak = Math.max(peak, running);
      return new Promise<void>((resolve) =>
        releases.push(() => {
          running -= 1;
          resolve();
        })
      );
    });
    runIdleQueue(tasks, 2);
    // Idle repeatedly with nothing finishing: only two may start.
    for (let i = 0; i < 6; i++) {
      goIdle();
      await flush();
    }
    expect(peak).toBe(2);
    expect(releases).toHaveLength(2);
    // Finishing one frees a slot for the next on the next idle slice.
    releases[0]();
    await flush();
    goIdle();
    await flush();
    expect(releases).toHaveLength(3);
    expect(peak).toBe(2);
  });

  it('runs every task eventually and survives a rejecting one', async () => {
    const done: number[] = [];
    runIdleQueue([
      async () => {
        done.push(1);
      },
      async () => {
        throw new Error('network');
      },
      async () => {
        done.push(3);
      },
    ]);
    for (let i = 0; i < 10; i++) {
      goIdle();
      await flush();
    }
    expect(done).toEqual([1, 3]);
  });

  it('cancel stops tasks that have not started and drops the pending idle callback', async () => {
    const first = vi.fn().mockResolvedValue(undefined);
    const second = vi.fn().mockResolvedValue(undefined);
    const cancel = runIdleQueue([first, second], 1);
    goIdle();
    await flush();
    expect(first).toHaveBeenCalledTimes(1);
    cancel();
    expect(idleCallbacks.size).toBe(0);
    goIdle();
    await flush();
    expect(second).not.toHaveBeenCalled();
  });
});
