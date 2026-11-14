import { get } from 'svelte/store';
import { isRequestCancelled } from '$lib/axios';
import { API_ERROR_CODE_KEYS, HOLD_REFUSAL_KEYS, holdAuthorityKey } from '$lib/i18n/keyMaps';
import { SUPPORT_ERROR_TOAST, supportGrantErrorCode } from '$lib/supportAccess/errors';
import { readAccountLifecycle } from '$stores/auth';
import { toastStore } from '$stores/toast';
import { t } from '$stores/locale';

interface RateLimitedError {
  retryAfterSeconds?: number;
}

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
  isAxiosError?: unknown;
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

const LEGAL_HOLD_ERROR_CODE = 'FILE_UNDER_LEGAL_HOLD';

/** The translated refusal for a `FILE_UNDER_LEGAL_HOLD` detail, or `null` for any other detail. */
function holdRefusalMessage(detail: unknown): string | null {
  const d = detail as { error?: unknown; held_by?: unknown } | null | undefined;
  if (!d || typeof d !== 'object' || d.error !== LEGAL_HOLD_ERROR_CODE) return null;
  const heldBy = Array.isArray(d.held_by) ? d.held_by.map(String) : [];
  const key = HOLD_REFUSAL_KEYS[holdAuthorityKey(heldBy)];
  // An unrecognised `held_by` falls back to the server's sentence rather than a dotted key.
  return key ? get(t)(key) : null;
}

/** The translated sentence for a refusal whose ``detail.code`` the client knows, else null. */
function codedRefusalMessage(detail: unknown): string | null {
  const code = (detail as { code?: unknown } | null | undefined)?.code;
  if (typeof code !== 'string' || !Object.hasOwn(API_ERROR_CODE_KEYS, code)) return null;
  const key = API_ERROR_CODE_KEYS[code];
  return key ? get(t)(key) : null;
}

/** A non-empty string, else undefined. */
function nonBlank(value: unknown): string | undefined {
  return typeof value === 'string' && value.trim() ? value : undefined;
}

// The refusals the client translates itself, from a stable code or the holder.
function translatedRefusal(error: unknown, detail: unknown): string | null {
  // A legal hold refusal (issue #1276) is translated here, once, from WHO holds the item: the
  // server's own sentence is English, and every delete, remove and reprocess site reads this.
  const held = holdRefusalMessage(detail);
  if (held) return held;
  // A support-session refusal is translated for the same reason; the interceptor in $lib/axios
  // shows the same sentence, so the toast store collapses the pair into one.
  const supportKey = SUPPORT_ERROR_TOAST[supportGrantErrorCode(error) ?? ''];
  if (supportKey) return get(t)(supportKey);
  // A refusal with a stable code we know (legal gate, invitation and verification links): the
  // sentence is ours, translated. Unknown codes fall through to the server's own message.
  return codedRefusalMessage(detail);
}

// FastAPI's default 422 shape is an ARRAY of validation-error objects
// (`detail: [{msg: "field required", ...}, ...]`). Without this the chain fell
// through to the generic fallback and dropped the actual field error.
function validationMessages(detail: unknown): string | undefined {
  if (!Array.isArray(detail)) return undefined;
  const joined = detail
    .map((d) => (d as { msg?: unknown })?.msg)
    .filter((msg): msg is string => typeof msg === 'string' && msg.trim() !== '')
    .join('. ');
  return joined || undefined;
}

// What the server said, in order of preference: a string or validation-array `detail`, an
// object detail's `message`, then a top-level `message`.
function serverMessage(e: AxiosLikeError | null | undefined, detail: unknown): string | undefined {
  return (
    nonBlank(detail) ??
    validationMessages(detail) ??
    // An account-lifecycle refusal carries an OBJECT detail (`{code, message}`).
    // Without this the chain fell through to `error.message` and surfaced the raw,
    // untranslated "Request failed with status code 403" instead of the reason.
    nonBlank((detail as { message?: unknown } | null | undefined)?.message) ??
    nonBlank(e?.response?.data?.message)
  );
}

/**
 * The error's own message, when it is worth showing. Axios fills `message` with its English
 * transport text ("Network Error", "Request failed with status code 500", "timeout of 30000ms
 * exceeded"), never anything a user should read, so an axios error contributes a translated
 * network sentence or nothing, and the caller's translated fallback speaks instead.
 */
function clientMessage(error: unknown): string | undefined {
  const e = error as AxiosLikeError | null | undefined;
  if (e?.isAxiosError === true) {
    return getErrorCode(error) === 'ERR_NETWORK' ? get(t)('common.networkError') : undefined;
  }
  return nonBlank(e?.message);
}

/**
 * Extracts a human-readable message from an unknown error. Order: a refusal the client
 * translates from a stable code (`API_ERROR_CODE_KEYS`, legal holds, support access), then the
 * server's own words (`response.data.detail`, `detail.message`, `response.data.message`), then
 * a non-axios error's `message` (or the translated network sentence for an unreachable server),
 * then the fallback. The default fallback is resolved per call, so it follows the active locale
 * rather than the one that was active when this module was imported.
 */
export function getErrorMessage(
  error: unknown,
  fallback = get(t)('common.somethingWentWrong')
): string {
  const e = error as AxiosLikeError;
  const detail = e?.response?.data?.detail;
  return (
    translatedRefusal(error, detail) ?? serverMessage(e, detail) ?? clientMessage(error) ?? fallback
  );
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
  if (!opts.silent) {
    // Issue #788: a rate-limited request carries a wait time ($lib/axios's
    // response interceptor already parsed `Retry-After` onto the error) —
    // render it via the toast's own hint slot instead of a generic error,
    // so "please wait a moment" becomes "please wait a moment, try again in
    // 12 seconds".
    const retryAfterSeconds = (error as RateLimitedError | null)?.retryAfterSeconds;
    if (getErrorStatus(error) === 429 && typeof retryAfterSeconds === 'number') {
      toastStore.warning(message, undefined, { retryAfterSeconds });
    } else {
      toastStore.error(message);
    }
  }
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
