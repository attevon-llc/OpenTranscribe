import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';
import { get } from 'svelte/store';
import { createRetryCountdown, parseRetryAfter } from './retryAfter';

describe('parseRetryAfter (#788)', () => {
  const now = Date.parse('2026-01-01T00:00:00Z');

  it.each([
    ['30', 30],
    [' 5 ', 5],
    [30, 30],
    ['0', null],
    ['1.5', null],
  ])('seconds form %j -> %j', (raw, expected) => {
    expect(parseRetryAfter(raw, now)).toBe(expected);
  });

  it('reads the HTTP-date form as seconds from now (rounded up)', () => {
    expect(parseRetryAfter('Thu, 01 Jan 2026 00:01:00 GMT', now)).toBe(60);
    expect(parseRetryAfter('Thu, 01 Jan 2026 00:00:00.5 GMT', now)).toBeNull(); // not a valid HTTP-date
  });

  it.each([
    undefined,
    null,
    '',
    'soon',
    'NaN',
    '-5',
    -5,
    NaN,
    Infinity,
    'Wed, 31 Dec 2025 00:00:00 GMT',
  ])('degrades to null for %j instead of NaN or a negative wait', (raw) => {
    expect(parseRetryAfter(raw, now)).toBeNull();
  });

  it('caps an absurd value at 24h', () => {
    expect(parseRetryAfter('999999999', now)).toBe(86400);
  });
});

describe('createRetryCountdown', () => {
  beforeEach(() => vi.useFakeTimers());
  afterEach(() => vi.useRealTimers());

  it('counts down to zero and stops', () => {
    const c = createRetryCountdown();
    expect(get(c.remaining)).toBe(0);
    c.start(3);
    expect(get(c.remaining)).toBe(3);
    vi.advanceTimersByTime(1000);
    expect(get(c.remaining)).toBe(2);
    vi.advanceTimersByTime(5000);
    expect(get(c.remaining)).toBe(0);
    expect(vi.getTimerCount()).toBe(0);
  });

  it('start(null) and stop() clear it', () => {
    const c = createRetryCountdown();
    c.start(10);
    c.start(null);
    expect(get(c.remaining)).toBe(0);
    c.start(10);
    c.stop();
    expect(get(c.remaining)).toBe(0);
    expect(vi.getTimerCount()).toBe(0);
  });
});
