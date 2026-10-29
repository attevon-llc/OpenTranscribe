import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';
import { createPoller } from './poller';

function setHidden(hidden: boolean) {
  Object.defineProperty(document, 'hidden', { configurable: true, get: () => hidden });
  document.dispatchEvent(new Event('visibilitychange'));
}

describe('createPoller', () => {
  beforeEach(() => {
    vi.useFakeTimers();
    setHidden(false);
  });
  afterEach(() => {
    vi.useRealTimers();
  });

  it('does not run on start, then runs once per interval', async () => {
    const fn = vi.fn();
    const p = createPoller(fn, { intervalMs: 1000 });
    p.start();
    expect(fn).not.toHaveBeenCalled();
    await vi.advanceTimersByTimeAsync(3000);
    expect(fn).toHaveBeenCalledTimes(3);
    p.stop();
  });

  it('stop ends the cadence and is idempotent; start is idempotent', async () => {
    const fn = vi.fn();
    const p = createPoller(fn, { intervalMs: 1000 });
    p.start();
    p.start();
    await vi.advanceTimersByTimeAsync(1000);
    expect(fn).toHaveBeenCalledTimes(1);
    p.stop();
    p.stop();
    expect(p.active).toBe(false);
    await vi.advanceTimersByTimeAsync(5000);
    expect(fn).toHaveBeenCalledTimes(1);
  });

  it('can be restarted after stop', async () => {
    const fn = vi.fn();
    const p = createPoller(fn, { intervalMs: 1000 });
    p.start();
    p.stop();
    p.start();
    await vi.advanceTimersByTimeAsync(1000);
    expect(fn).toHaveBeenCalledTimes(1);
    p.stop();
  });

  it('pauses while hidden and catches up once when visible again', async () => {
    const fn = vi.fn();
    const p = createPoller(fn, { intervalMs: 1000 });
    p.start();
    setHidden(true);
    await vi.advanceTimersByTimeAsync(10_000);
    expect(fn).not.toHaveBeenCalled();
    expect(p.active).toBe(true);
    setHidden(false);
    await vi.advanceTimersByTimeAsync(0);
    expect(fn).toHaveBeenCalledTimes(1);
    await vi.advanceTimersByTimeAsync(1000);
    expect(fn).toHaveBeenCalledTimes(2);
    p.stop();
  });

  it('runOnResume false resumes the cadence without an immediate run', async () => {
    const fn = vi.fn();
    const p = createPoller(fn, { intervalMs: 1000, runOnResume: false });
    p.start();
    setHidden(true);
    setHidden(false);
    await vi.advanceTimersByTimeAsync(0);
    expect(fn).not.toHaveBeenCalled();
    await vi.advanceTimersByTimeAsync(1000);
    expect(fn).toHaveBeenCalledTimes(1);
    p.stop();
  });

  it('started while hidden waits for visibility', async () => {
    setHidden(true);
    const fn = vi.fn();
    const p = createPoller(fn, { intervalMs: 1000 });
    p.start();
    await vi.advanceTimersByTimeAsync(5000);
    expect(fn).not.toHaveBeenCalled();
    setHidden(false);
    await vi.advanceTimersByTimeAsync(0);
    expect(fn).toHaveBeenCalledTimes(1);
    p.stop();
  });

  it('pauseWhenHidden false keeps polling in a hidden tab', async () => {
    const fn = vi.fn();
    const p = createPoller(fn, { intervalMs: 1000, pauseWhenHidden: false });
    p.start();
    setHidden(true);
    await vi.advanceTimersByTimeAsync(2000);
    expect(fn).toHaveBeenCalledTimes(2);
    p.stop();
  });

  it('stop removes the visibility listener (a later show does not run anything)', async () => {
    const fn = vi.fn();
    const p = createPoller(fn, { intervalMs: 1000 });
    p.start();
    setHidden(true);
    p.stop();
    setHidden(false);
    await vi.advanceTimersByTimeAsync(5000);
    expect(fn).not.toHaveBeenCalled();
  });

  it('never overlaps async runs: ticks during a pending run are skipped', async () => {
    let release: () => void = () => {};
    const fn = vi.fn(() => new Promise<void>((resolve) => (release = resolve)));
    const p = createPoller(fn, { intervalMs: 1000 });
    p.start();
    await vi.advanceTimersByTimeAsync(5000);
    expect(fn).toHaveBeenCalledTimes(1);
    release();
    await vi.advanceTimersByTimeAsync(1000);
    expect(fn).toHaveBeenCalledTimes(2);
    p.stop();
  });

  it('a rejecting callback is reported and the loop keeps going', async () => {
    const onError = vi.fn();
    const fn = vi.fn(() => Promise.reject(new Error('nope')));
    const p = createPoller(fn, { intervalMs: 1000, onError });
    p.start();
    await vi.advanceTimersByTimeAsync(3000);
    expect(fn).toHaveBeenCalledTimes(3);
    expect(onError).toHaveBeenCalledTimes(3);
    p.stop();
  });

  it('a synchronously throwing callback is reported and the loop keeps going', async () => {
    const onError = vi.fn();
    const fn = vi.fn(() => {
      throw new Error('sync');
    });
    const p = createPoller(fn, { intervalMs: 1000, onError });
    p.start();
    await vi.advanceTimersByTimeAsync(2000);
    expect(fn).toHaveBeenCalledTimes(2);
    expect(onError).toHaveBeenCalledTimes(2);
    p.stop();
  });

  it('stop from inside the callback ends the loop', async () => {
    const p = createPoller(
      () => {
        p.stop();
      },
      { intervalMs: 1000 }
    );
    const spy = vi.spyOn(p, 'stop');
    p.start();
    await vi.advanceTimersByTimeAsync(5000);
    expect(spy).toHaveBeenCalledTimes(1);
    expect(p.active).toBe(false);
  });
});
