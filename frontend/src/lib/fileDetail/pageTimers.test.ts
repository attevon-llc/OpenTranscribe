import { describe, it, expect, vi, beforeEach, afterEach } from 'vitest';
import { createPageTimers } from './pageTimers';

describe('createPageTimers', () => {
  beforeEach(() => vi.useFakeTimers());
  afterEach(() => vi.useRealTimers());

  it('runs a scheduled callback after its delay', () => {
    const timers = createPageTimers();
    const callback = vi.fn();
    timers.schedule(callback, 200);

    vi.advanceTimersByTime(199);
    expect(callback).not.toHaveBeenCalled();
    vi.advanceTimersByTime(1);
    expect(callback).toHaveBeenCalledTimes(1);
  });

  it('cancels every pending callback on destroy', () => {
    const timers = createPageTimers();
    const first = vi.fn();
    const second = vi.fn();
    timers.schedule(first, 100);
    timers.schedule(second, 2000);
    expect(vi.getTimerCount()).toBe(2);

    timers.destroy();

    expect(vi.getTimerCount()).toBe(0);
    vi.advanceTimersByTime(5000);
    expect(first).not.toHaveBeenCalled();
    expect(second).not.toHaveBeenCalled();
  });

  it('refuses to schedule once destroyed (a late fetch resolving after teardown)', () => {
    const timers = createPageTimers();
    timers.destroy();
    const callback = vi.fn();

    timers.schedule(callback, 10);

    expect(vi.getTimerCount()).toBe(0);
    vi.advanceTimersByTime(100);
    expect(callback).not.toHaveBeenCalled();
  });

  it('forgets a callback once it has fired, so destroy has nothing stale to clear', () => {
    const timers = createPageTimers();
    const clearSpy = vi.spyOn(globalThis, 'clearTimeout');
    timers.schedule(() => {}, 10);
    vi.advanceTimersByTime(10);
    expect(vi.getTimerCount()).toBe(0);

    timers.destroy();

    expect(clearSpy).not.toHaveBeenCalled();
    clearSpy.mockRestore();
  });
});
