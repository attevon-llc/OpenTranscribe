/**
 * User-facing results for failed auth requests: login, registration, password change,
 * PKI, MFA verification and MFA enrolment. Pure; the caller passes the current
 * `Translate` (`get(t)`) and keeps the request, the logging and every state change.
 *
 * Callers key their UI off the HTTP status, never off substrings of a message: the
 * messages are localised.
 */
import type { Translate } from '$lib/i18n';
import { retryAfterFromHeaders } from '$lib/utils/retryAfter';

/**
 * Minimal shape of an axios error used for status- and detail-based message
 * extraction in this module. Mirrors the relevant subset of AxiosError.
 */
export interface AuthRequestError {
  response?: {
    status?: number;
    headers?: unknown;
    data?: { detail?: unknown; message?: unknown };
  };
  request?: unknown;
  message?: string;
}

export function asAuthError(error: unknown): AuthRequestError {
  return (error ?? {}) as AuthRequestError;
}

/** Message and `Retry-After` seconds for a failed `POST /auth/login`. */
export function loginFailure(
  err: AuthRequestError,
  translate: Translate
): { message: string; retryAfter: number | null } {
  // Extract meaningful error message from backend response
  let errorMessage = translate('auth.error.loginFailedCheckCredentials');
  let retryAfter: number | null = null;

  if (err.response) {
    // Server responded with an error status
    switch (err.response.status) {
      case 401:
        errorMessage =
          (err.response.data?.detail as string) || translate('auth.error.invalidCredentials');
        break;
      case 400:
        errorMessage =
          (err.response.data?.detail as string) || translate('auth.error.invalidRequest');
        break;
      case 429:
        retryAfter = retryAfterFromHeaders(err.response.headers);
        errorMessage =
          retryAfter !== null
            ? translate('auth.error.tooManyLoginAttemptsWait', { seconds: retryAfter })
            : translate('auth.error.tooManyLoginAttempts');
        break;
      case 500:
      case 502:
      case 503:
        errorMessage = translate('auth.error.serverError');
        break;
      default:
        errorMessage =
          (err.response.data?.detail as string) ||
          (err.response.data?.message as string) ||
          translate('auth.error.loginFailed');
    }
  } else if (err.request) {
    // Network error - no response received
    errorMessage = translate('auth.error.networkUnreachable');
  } else if (err.message) {
    // Something else happened
    errorMessage = translate('auth.error.unexpected');
  }

  return { message: errorMessage, retryAfter };
}

/** Message for a failed self-service password change (`PUT /users/me`). */
export function passwordChangeFailure(rawError: unknown, translate: Translate): string {
  const detail = asAuthError(rawError).response?.data?.detail;

  if (typeof detail === 'string') return detail;
  if (Array.isArray(detail)) {
    return detail
      .map((item: { msg?: string }) => item?.msg ?? String(item))
      .filter(Boolean)
      .join('. ');
  }
  return translate('auth.forcedChange.failed');
}

/** Message for a failed `POST /auth/register`. */
export function registrationFailure(error: unknown, translate: Translate): string {
  // Handle validation errors (array) vs simple error messages (string)
  let errorMessage = translate('auth.error.registrationFailed');
  const detail = asAuthError(error).response?.data?.detail;

  if (detail) {
    if (typeof detail === 'string') {
      errorMessage = detail;
    } else if (Array.isArray(detail)) {
      // Pydantic validation errors - extract messages
      errorMessage = detail
        .map((item: { msg?: string; message?: string }) => item.msg || item.message || String(item))
        .join('. ');
    } else if (typeof detail === 'object') {
      const d = detail as { msg?: string; message?: string };
      errorMessage = d.msg || d.message || JSON.stringify(detail);
    }
  }

  return errorMessage;
}

/** Message for a failed `POST /auth/pki/authenticate`. */
export function pkiLoginFailure(rawError: unknown, translate: Translate): string {
  const error = asAuthError(rawError);
  let message = translate('auth.error.pkiFailed');
  if (error.response?.status === 401) {
    message = translate('auth.error.pkiCertificateMissing');
  } else if (error.response?.status === 400) {
    message = (error.response?.data?.detail as string) || translate('auth.error.pkiNotEnabled');
  }
  return message;
}

/** Message for a failed `POST /auth/mfa/verify` (the login-time TOTP challenge). */
export function mfaVerifyFailure(rawError: unknown, translate: Translate): string {
  const error = asAuthError(rawError);
  let message = translate('auth.error.mfaFailed');
  if (error.response?.status === 401) {
    message = (error.response?.data?.detail as string) || translate('auth.error.mfaInvalidCode');
  } else if (error.response?.status === 400) {
    message = (error.response?.data?.detail as string) || translate('auth.error.mfaInvalidToken');
  } else if (error.response?.status === 429) {
    message = translate('auth.error.mfaTooManyAttempts');
  }
  return message;
}

/**
 * How the UI should react to a failed enrolment call.
 *
 * - `retry`       — wrong code (or an unclassified 4xx); the half-token is still
 *                   good, let the user try again in place.
 * - `expired`     — the half-token is spent or past its lifetime; only a fresh
 *                   login can mint another one.
 * - `restart`     — server state moved on (already enabled / setup not
 *                   initiated); re-run /mfa/setup.
 * - `unavailable` — this account can never enrol here (external IdP owns the
 *                   second factor, or MFA is off system-wide). Not a retry.
 */
type MfaEnrollmentErrorKind = 'retry' | 'expired' | 'restart' | 'unavailable';

export interface MfaEnrollmentError {
  kind: MfaEnrollmentErrorKind;
  message: string;
}

export function classifyEnrollmentError(
  rawError: unknown,
  translate: Translate
): MfaEnrollmentError {
  const error = asAuthError(rawError);
  const status = error.response?.status;
  const detail = typeof error.response?.data?.detail === 'string' ? error.response.data.detail : '';

  // 401 == the half-token was already claimed or has expired. (The axios
  // response interceptor first attempts a cookie refresh, which cannot succeed
  // during enrolment and rejects with its own 401 — either way we land here.)
  if (status === 401) {
    return { kind: 'expired', message: translate('auth.mfaEnroll.error.tokenExpired') };
  }

  if (status === 400) {
    // The wire carries no error code, so the API's own English `detail` is the
    // only signal separating "type the code again" from "this account can never
    // enrol". Match narrowly and treat anything unrecognised as a retry — the
    // safe default, since it leaves the user in the flow.
    const lower = detail.toLowerCase();
    if (
      lower.includes('not available for your authentication type') ||
      lower.includes('not enabled on this system')
    ) {
      return { kind: 'unavailable', message: translate('auth.mfaEnroll.error.unavailable') };
    }
    if (lower.includes('already enabled') || lower.includes('setup not initiated')) {
      return { kind: 'restart', message: translate('auth.mfaEnroll.error.restart') };
    }
    return { kind: 'retry', message: translate('auth.mfaEnroll.error.invalidCode') };
  }

  return { kind: 'retry', message: detail || translate('auth.mfaEnroll.error.setupFailed') };
}
