import type { AxiosError, InternalAxiosRequestConfig } from 'axios';

/** The request config carrying the replay-once flag. */
export type NetworkRetryConfig = InternalAxiosRequestConfig & { _networkRetry?: boolean };

/** Safe methods only: a write that may have reached the server is never sent twice. */
const NETWORK_RETRY_METHODS = new Set(['GET', 'HEAD', 'OPTIONS']);
/** Wait before the replay, plus up to NETWORK_RETRY_JITTER_MS so a burst does not replay in step. */
export const NETWORK_RETRY_DELAY_MS = 300;
export const NETWORK_RETRY_JITTER_MS = 200;

/**
 * True for a request that got no HTTP response at all (axios `ERR_NETWORK`: "the request was
 * made but no response was received", https://axios.rest/pages/advanced/error-handling).
 * A cancellation (`ERR_CANCELED`) and a timeout (`ECONNABORTED`) are different codes.
 */
function isTransportFailure(error: AxiosError): boolean {
  return !error.response && error.code === 'ERR_NETWORK';
}

/**
 * Replay a safe-method request ONCE after a transport failure, or return `undefined`.
 *
 * Why: Chromium fails every request still waiting for a connection with
 * `net::ERR_NETWORK_CHANGED` whenever the machine's network interfaces change (Wi-Fi roaming, a
 * VPN connecting, Docker creating or removing a bridge), and XHR reports it as a plain network
 * error. Those requests never reached the server, so the page showed "could not load" while the
 * API was healthy. One quick replay recovers; a failure that persists still rejects, so a real
 * outage still shows its error state.
 *
 * The pattern (a flag on the config, re-sent through the instance, no-response condition) is
 * axios's own retry recipe, https://axios.rest/pages/advanced/retry, limited here to one replay
 * of safe methods, as `busyRetry` does for 503. axios has no built-in retry, and the axios-retry
 * package is archived (no further releases), so this stays a few lines of our own.
 */
export function replayAfterTransportFailure(
  error: AxiosError,
  send: (config: NetworkRetryConfig) => Promise<unknown>
): Promise<unknown> | undefined {
  if (!isTransportFailure(error)) return undefined;
  const cfg = error.config as NetworkRetryConfig | undefined;
  const method = String(cfg?.method ?? 'get').toUpperCase();
  if (!cfg || cfg._networkRetry || !NETWORK_RETRY_METHODS.has(method)) return undefined;
  cfg._networkRetry = true;
  const waitMs = NETWORK_RETRY_DELAY_MS + Math.random() * NETWORK_RETRY_JITTER_MS;
  return new Promise((resolve) => setTimeout(resolve, waitMs)).then(() => send(cfg));
}
