/**
 * Standard, dependency-free error primitives.
 *
 * Complements `$lib/utils/apiError.ts` (which extracts messages from axios errors and
 * surfaces toasts). This module is intentionally transport-agnostic: it gives any code
 * path a single normalized error shape (`AppError`) and a `toAppError` coercion so
 * `catch (e: unknown)` blocks can work with a typed value without pulling in axios/toast.
 */

/**
 * Application error with an optional HTTP-style `status` and a preserved `cause`.
 *
 * `cause` is exposed as a typed field for our own inspection regardless of whether the
 * runtime's `Error` supports the native `cause` option.
 */
export class AppError extends Error {
  /** Optional HTTP-style status code (e.g. 404, 500) associated with the failure. */
  readonly status?: number;
  /** The original thrown value this error was derived from, if any. */
  readonly cause?: unknown;

  constructor(message: string, options: { status?: number; cause?: unknown } = {}) {
    super(message);
    this.name = 'AppError';
    this.status = options.status;
    this.cause = options.cause;
    // Restore the prototype chain when targeting older transpile targets.
    Object.setPrototypeOf(this, AppError.prototype);
  }
}

interface ErrorLike {
  message?: unknown;
  status?: unknown;
  response?: { status?: unknown; data?: { detail?: unknown; message?: unknown } };
}

/** Extracts a numeric status from common error shapes (`status` or `response.status`). */
function extractStatus(value: ErrorLike): number | undefined {
  const direct = value.status;
  if (typeof direct === 'number' && Number.isFinite(direct)) return direct;
  const nested = value.response?.status;
  if (typeof nested === 'number' && Number.isFinite(nested)) return nested;
  return undefined;
}

/** Extracts a human-readable message, preferring FastAPI `detail`, then `message`. */
function extractMessage(value: ErrorLike): string | undefined {
  const detail = value.response?.data?.detail;
  if (typeof detail === 'string' && detail.trim()) return detail.trim();
  const respMessage = value.response?.data?.message;
  if (typeof respMessage === 'string' && respMessage.trim()) return respMessage.trim();
  if (typeof value.message === 'string' && value.message.trim()) return value.message.trim();
  return undefined;
}

/**
 * Normalizes any thrown value into an `AppError`.
 *
 * - An existing `AppError` is returned unchanged.
 * - A `string` becomes the error message.
 * - An `Error` / axios-like object contributes its message, status, and is kept as `cause`.
 * - Anything else falls back to `fallback`.
 */
export function toAppError(value: unknown, fallback = 'Something went wrong'): AppError {
  if (value instanceof AppError) return value;

  if (typeof value === 'string') {
    const message = value.trim();
    return new AppError(message || fallback);
  }

  if (value && typeof value === 'object') {
    const like = value as ErrorLike;
    const message = extractMessage(like) ?? fallback;
    return new AppError(message, { status: extractStatus(like), cause: value });
  }

  return new AppError(fallback, { cause: value });
}
