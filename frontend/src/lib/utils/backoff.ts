/**
 * Reconnect backoff with jitter.
 *
 * Pure helpers — no timers, no state. The caller owns the `setTimeout`.
 */

/** First retry waits this long (attempt 1). */
export const BASE_RECONNECT_DELAY_MS = 1000;

/** Backoff never grows past this, no matter how many attempts have failed. */
export const MAX_RECONNECT_DELAY_MS = 30000;

/**
 * Exponential backoff delay for a reconnect attempt, **with equal jitter**.
 *
 * Without jitter every client that was connected when the server went down
 * re-attempts on the same 2/4/8/16/30-second grid, so the server is hit by a
 * synchronised burst on each tick and knocks itself over again. Equal jitter
 * spreads those retries over the second half of each backoff window: the delay
 * is uniformly distributed in `[base/2, base)`, which keeps a guaranteed
 * minimum wait (unlike full jitter, which can return ~0 ms and hammer a server
 * that is still starting) while decorrelating clients from each other.
 *
 * @param attempt - 1-based attempt number. Values below 1 are clamped to 1.
 * @param random - Injectable uniform source in `[0, 1)`; defaults to `Math.random`.
 *                 Tests pass a deterministic value.
 * @returns Delay in milliseconds, rounded to an integer.
 */
export function reconnectDelayMs(attempt: number, random: () => number = Math.random): number {
  const safeAttempt = Number.isFinite(attempt) ? Math.max(1, Math.floor(attempt)) : 1;
  // Cap the exponent before the shift so 2 ** attempt can't overflow to Infinity.
  const exponential = 2 ** Math.min(safeAttempt, 10) * BASE_RECONNECT_DELAY_MS;
  const base = Math.min(exponential, MAX_RECONNECT_DELAY_MS);
  const half = base / 2;
  return Math.round(half + random() * half);
}
