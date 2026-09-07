import { get } from 'svelte/store';
import { isRequestCancelled } from '$lib/axios';
import { readAccountLifecycle } from '$stores/auth';
import { toastStore } from '$stores/toast';
import { t } from '$stores/locale';

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
 * then the fallback. The default fallback is resolved per call, so it follows the
 * active locale rather than the one that was active when this module was imported.
 */
export function getErrorMessage(
  error: unknown,
  fallback = get(t)('common.somethingWentWrong')
): string {
  const e = error as AxiosLikeError;
  const detail = e?.response?.data?.detail;
  if (typeof detail === 'string' && detail.trim()) return detail;
  // FastAPI's default 422 shape is an ARRAY of validation-error objects
  // (`detail: [{msg: "field required", ...}, ...]`). Without this the chain fell
  // through to the generic fallback and dropped the actual field error.
  if (Array.isArray(detail)) {
    const joined = detail
      .map((d) => (d as { msg?: unknown })?.msg)
      .filter((msg): msg is string => typeof msg === 'string' && msg.trim() !== '')
      .join('. ');
    if (joined) return joined;
  }
  // An account-lifecycle refusal carries an OBJECT detail (`{code, message}`).
  // Without this the chain fell through to `error.message` and surfaced the raw,
  // untranslated "Request failed with status code 403" instead of the reason.
  const detailMessage = (detail as { message?: unknown } | null | undefined)?.message;
  if (typeof detailMessage === 'string' && detailMessage.trim()) return detailMessage;
  const message = e?.response?.data?.message;
  if (typeof message === 'string' && message.trim()) return message;
  if (typeof e?.message === 'string' && e.message.trim()) return e.message;
  return fallback;
}

/**
 * Standard catch handler: silently ignores cancelled requests (logout/navigation aborts),
 * otherwise surfaces a toast. Returns the resolved message so callers can also use it inline.
 *
 * Account-lifecycle refusals are ignored for the same reason as cancellations: the app is
 * already handling them somewhere else. `$stores/auth` renders them as a **blocking screen**,
 * and no route is exempt from the approval gate — so every in-flight request answers 403 and
 * a toast per request would stack up in front of the very screen that explains the situation.
 */
export function handleApiError(
  error: unknown,
  fallback?: string,
  opts: { silent?: boolean } = {}
): string {
  const message = getErrorMessage(error, fallback);
  if (isRequestCancelled(error)) return message;
  if (readAccountLifecycle(error)) return message;
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
