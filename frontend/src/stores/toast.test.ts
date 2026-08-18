/**
 * `toastStore` backs transient success/error/warning/info toasts. These tests
 * pin the two behaviors most likely to silently regress: the per-toast
 * auto-dismiss `setTimeout` (armed only when `duration > 0`), and `error()`'s
 * `??` default duration — using `||` there would silently promote a
 * legitimate `duration: 0` ("never auto-dismiss") up to the 8s default, which
 * the source's own comment rules out. Fake timers are used throughout so a
 * stray real `setTimeout` from `show()`'s default 3000ms never outlives a test.
 */
import { describe, it, expect, vi, beforeEach, afterEach } from 'vitest';
import { get } from 'svelte/store';
import { toastStore } from './toast';

beforeEach(() => {
  vi.useFakeTimers();
  toastStore.clear();
});

afterEach(() => {
  vi.useRealTimers();
});

describe('show', () => {
  it('assigns a unique, monotonically increasing id per toast (Date.now-counter)', () => {
    toastStore.show('first');
    toastStore.show('second');

    const [first, second] = get(toastStore);
    expect(first.id).not.toBe(second.id);

    // Format is `${Date.now()}-${counter}`; with fake timers frozen, Date.now()
    // is identical for both, so the counter suffix is what guarantees order.
    const firstCounter = Number(first.id.split('-').pop());
    const secondCounter = Number(second.id.split('-').pop());
    expect(secondCounter).toBeGreaterThan(firstCounter);
  });

  it('defaults to type "success" and duration 3000', () => {
    toastStore.show('hello');

    const [toast] = get(toastStore);
    expect(toast.type).toBe('success');
    expect(toast.duration).toBe(3000);
    expect(toast.message).toBe('hello');
  });

  it('auto-dismisses after the given duration elapses, and not a moment sooner', () => {
    toastStore.show('bye', 'info', 1000);
    expect(get(toastStore)).toHaveLength(1);

    vi.advanceTimersByTime(999);
    expect(get(toastStore)).toHaveLength(1);

    vi.advanceTimersByTime(1);
    expect(get(toastStore)).toHaveLength(0);
  });

  it('never schedules a dismiss when duration is 0', () => {
    toastStore.show('sticky', 'info', 0);

    vi.advanceTimersByTime(1_000_000);
    expect(get(toastStore)).toHaveLength(1);
  });
});

describe('dismiss', () => {
  it('removes only the matching toast', () => {
    toastStore.show('a', 'info', 0);
    toastStore.show('b', 'info', 0);
    const [a, b] = get(toastStore);

    toastStore.dismiss(a.id);

    const remaining = get(toastStore);
    expect(remaining).toHaveLength(1);
    expect(remaining[0].id).toBe(b.id);
  });
});

describe('clear', () => {
  it('removes every toast, including ones with a pending auto-dismiss timer', () => {
    toastStore.show('a', 'info', 5000);
    toastStore.show('b', 'info', 0);

    toastStore.clear();

    expect(get(toastStore)).toEqual([]);
  });
});

describe('error', () => {
  it('defaults to an 8000ms duration when none is given', () => {
    toastStore.error('oops');

    const [toast] = get(toastStore);
    expect(toast.type).toBe('error');
    expect(toast.duration).toBe(8000);
  });

  it('uses ?? (not ||), so an explicit duration of 0 survives instead of falling back to 8000', () => {
    toastStore.error('persistent error', 0);

    const [toast] = get(toastStore);
    expect(toast.duration).toBe(0);

    // Pin the other half of the ?? contract: it must not schedule a dismiss either.
    vi.advanceTimersByTime(1_000_000);
    expect(get(toastStore)).toHaveLength(1);
  });

  it('honours an explicit non-zero duration instead of the 8000ms default', () => {
    toastStore.error('custom', 500);

    const [toast] = get(toastStore);
    expect(toast.duration).toBe(500);
  });
});

describe('success / warning / info', () => {
  it('set the corresponding type and pass an explicit duration through to show()', () => {
    toastStore.success('s', 111);
    toastStore.warning('w', 222);
    toastStore.info('i', 333);

    const [s, w, i] = get(toastStore);
    expect(s.type).toBe('success');
    expect(s.duration).toBe(111);
    expect(w.type).toBe('warning');
    expect(w.duration).toBe(222);
    expect(i.type).toBe('info');
    expect(i.duration).toBe(333);
  });

  it("fall back to show()'s default duration (3000) when none is given", () => {
    toastStore.success('s');
    toastStore.warning('w');
    toastStore.info('i');

    expect(get(toastStore).map((t) => t.duration)).toEqual([3000, 3000, 3000]);
  });
});
