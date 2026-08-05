import { describe, expect, it } from 'vitest';
import { BASE_RECONNECT_DELAY_MS, MAX_RECONNECT_DELAY_MS, reconnectDelayMs } from './backoff';

describe('reconnectDelayMs', () => {
  it('grows exponentially from the base delay', () => {
    // random() === 1 yields the top of the jitter window, i.e. the raw base delay.
    expect(reconnectDelayMs(1, () => 1)).toBe(2 * BASE_RECONNECT_DELAY_MS);
    expect(reconnectDelayMs(2, () => 1)).toBe(4 * BASE_RECONNECT_DELAY_MS);
    expect(reconnectDelayMs(3, () => 1)).toBe(8 * BASE_RECONNECT_DELAY_MS);
  });

  it('caps the delay at the ceiling however many attempts have failed', () => {
    expect(reconnectDelayMs(20, () => 1)).toBe(MAX_RECONNECT_DELAY_MS);
    expect(reconnectDelayMs(1000, () => 1)).toBe(MAX_RECONNECT_DELAY_MS);
    expect(reconnectDelayMs(Number.MAX_SAFE_INTEGER, () => 1)).toBe(MAX_RECONNECT_DELAY_MS);
  });

  it('applies equal jitter — never below half the window, never above it', () => {
    for (let attempt = 1; attempt <= 12; attempt++) {
      const base = Math.min(
        2 ** Math.min(attempt, 10) * BASE_RECONNECT_DELAY_MS,
        MAX_RECONNECT_DELAY_MS
      );
      expect(reconnectDelayMs(attempt, () => 0)).toBe(base / 2);
      expect(reconnectDelayMs(attempt, () => 1)).toBe(base);
      expect(reconnectDelayMs(attempt, () => 0.5)).toBe(base * 0.75);
    }
  });

  it('spreads real random draws across the jitter window (no thundering herd)', () => {
    const draws = new Set<number>();
    for (let i = 0; i < 200; i++) draws.add(reconnectDelayMs(4));
    // Attempt 4 → 16 s window, so jitter range [8000, 16000]. A fixed-grid
    // backoff would collapse to one value; jitter must produce many distinct
    // delays so reconnecting clients don't arrive in lockstep.
    expect(draws.size).toBeGreaterThan(50);
    for (const d of draws) {
      expect(d).toBeGreaterThanOrEqual(8000);
      expect(d).toBeLessThanOrEqual(16000);
    }
  });

  it('clamps nonsense attempt numbers to the first attempt', () => {
    expect(reconnectDelayMs(0, () => 1)).toBe(2 * BASE_RECONNECT_DELAY_MS);
    expect(reconnectDelayMs(-5, () => 1)).toBe(2 * BASE_RECONNECT_DELAY_MS);
    expect(reconnectDelayMs(Number.NaN, () => 1)).toBe(2 * BASE_RECONNECT_DELAY_MS);
  });
});
