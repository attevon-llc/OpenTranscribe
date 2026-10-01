import { writable, type Readable } from 'svelte/store';

const MAX_WAIT_SECONDS = 86400;

/**
 * Parse a `Retry-After` header into whole seconds, or `null` when it is absent or
 * unusable so callers fall back to the generic message instead of rendering `NaN`
 * (#788). Handles the delay-seconds form and the HTTP-date form (RFC 9110 10.2.3).
 */
export function parseRetryAfter(raw: unknown, now: number = Date.now()): number | null {
  if (typeof raw === 'number') {
    return Number.isFinite(raw) && Number.isInteger(raw) && raw > 0
      ? Math.min(raw, MAX_WAIT_SECONDS)
      : null;
  }
  if (typeof raw !== 'string') return null;
  const value = raw.trim();
  if (!value) return null;

  if (/^\d+$/.test(value)) {
    const seconds = Number(value);
    return seconds > 0 ? Math.min(seconds, MAX_WAIT_SECONDS) : null;
  }

  // HTTP-date always carries a weekday and a GMT zone; Date.parse also accepts far
  // looser strings ("soon" is NaN, but "5" is year 2001), so require the shape.
  if (!/^[A-Za-z]{3},\s\d{1,2}\s[A-Za-z]{3}\s\d{4}\s\d{2}:\d{2}:\d{2}\sGMT$/.test(value)) {
    return null;
  }
  const at = Date.parse(value);
  if (Number.isNaN(at)) return null;
  const seconds = Math.ceil((at - now) / 1000);
  return seconds > 0 ? Math.min(seconds, MAX_WAIT_SECONDS) : null;
}

/** Read `Retry-After` from axios-style headers (plain object or AxiosHeaders). */
export function retryAfterFromHeaders(headers: unknown): number | null {
  if (!headers || typeof headers !== 'object') return null;
  const h = headers as { get?: (name: string) => unknown } & Record<string, unknown>;
  const raw = typeof h.get === 'function' ? h.get('retry-after') : undefined;
  return parseRetryAfter(raw ?? h['retry-after'] ?? h['Retry-After']);
}

export interface RetryCountdown {
  /** Whole seconds left; 0 when idle. */
  remaining: Readable<number>;
  start(seconds: number | null | undefined): void;
  stop(): void;
}

export function createRetryCountdown(): RetryCountdown {
  const { subscribe, set } = writable(0);
  let left = 0;
  let timer: ReturnType<typeof setInterval> | null = null;

  function stop(): void {
    if (timer !== null) clearInterval(timer);
    timer = null;
    left = 0;
    set(0);
  }

  function start(seconds: number | null | undefined): void {
    stop();
    if (!seconds || seconds <= 0) return;
    left = Math.ceil(seconds);
    set(left);
    timer = setInterval(() => {
      left -= 1;
      if (left <= 0) stop();
      else set(left);
    }, 1000);
  }

  return { remaining: { subscribe }, start, stop };
}
