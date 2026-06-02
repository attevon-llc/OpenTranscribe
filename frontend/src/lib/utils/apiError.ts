import { isRequestCancelled } from '$lib/axios';
import { toastStore } from '$stores/toast';

/**
 * Standardized API error handling. OPT-IN — use in new / refactored code; the existing
 * ~600 hand-rolled try/catch→toast sites are migrated opportunistically, not en masse.
 *
 * Composes with the shared axios instance ($lib/axios), which already handles CSRF and
 * 401 refresh, and with the toast store ($stores/toast).
 */

interface AxiosLikeError {
  response?: { status?: number; data?: { detail?: unknown; message?: unknown } };
  message?: unknown;
  code?: unknown;
}

/**
 * Extracts the HTTP status code from an unknown (axios-like) error, or `undefined`
 * if there is none. Use this instead of casting to `any` for `err.response?.status`
 * checks in catch blocks.
 */
export function getErrorStatus(error: unknown): number | undefined {
  const status = (error as AxiosLikeError)?.response?.status;
  return typeof status === 'number' ? status : undefined;
}

/**
 * Extracts the axios error `code` (e.g. `ERR_NETWORK`, `ERR_CANCELED`,
 * `ECONNABORTED`) from an unknown error, or `undefined`.
 */
export function getErrorCode(error: unknown): string | undefined {
  const code = (error as AxiosLikeError)?.code;
  return typeof code === 'string' ? code : undefined;
}

/**
 * Extracts a human-readable message from an unknown error, preferring the FastAPI
 * `response.data.detail` shape, then `response.data.message`, then `error.message`,
 * then the fallback.
 */
export function getErrorMessage(error: unknown, fallback = 'Something went wrong'): string {
  const e = error as AxiosLikeError;
  const detail = e?.response?.data?.detail;
  if (typeof detail === 'string' && detail.trim()) return detail;
  const message = e?.response?.data?.message;
  if (typeof message === 'string' && message.trim()) return message;
  if (typeof e?.message === 'string' && e.message.trim()) return e.message;
  return fallback;
}

/**
 * Standard catch handler: silently ignores cancelled requests (logout/navigation aborts),
 * otherwise surfaces a toast. Returns the resolved message so callers can also use it inline.
 */
export function handleApiError(
  error: unknown,
  fallback?: string,
  opts: { silent?: boolean } = {}
): string {
  const message = getErrorMessage(error, fallback);
  if (isRequestCancelled(error)) return message;
  if (!opts.silent) toastStore.error(message);
  return message;
}

/**
 * Wraps an async operation with the standard error handling (and optional success toast).
 * Returns the result, or `undefined` if it failed. Re-throws nothing — the caller decides
 * what to do with `undefined`.
 */
export async function withAsync<T>(
  fn: () => Promise<T>,
  opts: { errorMsg: string; successMsg?: string; silent?: boolean; onFinally?: () => void }
): Promise<T | undefined> {
  try {
    const result = await fn();
    if (opts.successMsg) toastStore.success(opts.successMsg);
    return result;
  } catch (error) {
    handleApiError(error, opts.errorMsg, { silent: opts.silent });
    return undefined;
  } finally {
    opts.onFinally?.();
  }
}
