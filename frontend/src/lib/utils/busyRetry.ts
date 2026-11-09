import type { AxiosError, InternalAxiosRequestConfig } from 'axios';
import { get } from 'svelte/store';
import { t } from '$stores/locale';
import { retryAfterFromHeaders } from '$lib/utils/retryAfter';

/** The request config carrying the replay-once flag. */
export type BusyRetryConfig = InternalAxiosRequestConfig & { _busyRetry?: boolean };

/** Methods safe to replay after a 503 (safe methods; never POST/PATCH/DELETE). */
const BUSY_RETRY_METHODS = new Set(['GET', 'HEAD', 'OPTIONS']);
/** Longest Retry-After honoured; a larger value is clamped, jitter is added on top. */
const BUSY_RETRY_MAX_WAIT_S = 10;

/**
 * Server busy (scale lane A2): a 503 carrying Retry-After means "overloaded, come back
 * shortly", NOT "your session is gone" (Retry-After forms, delay-seconds or HTTP-date:
 * https://developer.mozilla.org/en-US/docs/Web/HTTP/Reference/Headers/Retry-After).
 * Read methods are replayed ONCE by re-sending error.config through `send`, tracked by a flag
 * on the config (the pattern on https://axios.rest/pages/advanced/retry). Method safety and
 * the jitter are our own design: that page does not check methods and MDN gives no client
 * guidance. Writes reject with `retryAfterSeconds` set and `getErrorMessage` renders the
 * translated "server is busy" sentence.
 *
 * Lives in its own module and is imported on the first 503, so the retry path stays out of the
 * first-load closure. Returns `undefined` when the 503 carries no Retry-After (not a busy signal).
 */
export function handleBusy(
  error: AxiosError & { retryAfterSeconds?: number },
  send: (config: BusyRetryConfig) => Promise<unknown>
) {
  const busyWait = retryAfterFromHeaders(error.response?.headers);
  if (busyWait === null || !error.response) return undefined;
  error.retryAfterSeconds = busyWait;
  // Ours to word, and translated: `getErrorMessage` reads `detail`, so every caller shows it.
  error.response.data = {
    ...(error.response.data as Record<string, unknown>),
    detail: get(t)('common.serverBusy'),
  };
  const cfg = error.config as BusyRetryConfig | undefined;
  const method = String(cfg?.method ?? 'get').toUpperCase();
  if (cfg && !cfg._busyRetry && BUSY_RETRY_METHODS.has(method)) {
    cfg._busyRetry = true;
    const waitMs = Math.min(busyWait, BUSY_RETRY_MAX_WAIT_S) * 1000 + Math.random() * 1000;
    return new Promise((resolve) => setTimeout(resolve, waitMs)).then(() => send(cfg));
  }
  return Promise.reject(error);
}
