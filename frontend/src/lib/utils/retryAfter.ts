/**
 * Parse an RFC 9110 §10.2.3 `Retry-After` header value.
 *
 * The header is either delta-seconds (a non-negative integer) or an HTTP-date.
 * Per the issue's own acceptance criterion, a header that is absent or
 * unparseable must degrade to the caller's generic message rather than
 * rendering `NaN` — so every failure path here returns `null`, never `NaN` or
 * a throw.
 *
 * @param value The raw header value (`fetch`'s `Headers.get` return, or
 *   axios's lower-cased `response.headers['retry-after']`), or `null`/`undefined`.
 * @returns Seconds to wait, or `null` if the header is absent, empty,
 *   negative, non-integer, an unparseable string, or an HTTP-date already in
 *   the past (nothing left to wait for).
 */
export function parseRetryAfter(value: string | null | undefined): number | null {
  if (value == null) return null;

  const trimmed = value.trim();
  if (trimmed === '') return null;

  // delta-seconds: RFC 9110 restricts this to a non-negative integer. Reject
  // "-5" and "3.7" here rather than letting them fall through to Date.parse,
  // which would otherwise misparse "-5" as NaN (harmless) but could accept
  // other numeric-looking strings as dates on some engines.
  if (/^\d+$/.test(trimmed)) {
    const seconds = Number(trimmed);
    return Number.isFinite(seconds) ? seconds : null;
  }

  const dateMs = Date.parse(trimmed);
  if (Number.isNaN(dateMs)) return null;

  const deltaSeconds = Math.round((dateMs - Date.now()) / 1000);
  return deltaSeconds > 0 ? deltaSeconds : null;
}
