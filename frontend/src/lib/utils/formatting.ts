/**
 * Frontend-only formatting utilities.
 *
 * Note: Basic formatting (duration, file size, dates) is now handled by the backend.
 * These utilities are kept only for UI-specific formatting needs.
 */

/**
 * Formats a date string to a locale-aware short date.
 */
export function formatDate(dateStr: string): string {
  try {
    return new Date(dateStr).toLocaleDateString();
  } catch {
    return dateStr;
  }
}

/**
 * Returns 1-2 character initials from a name or email.
 */
export function getInitials(name: string | null, email: string): string {
  if (name) {
    return name
      .split(' ')
      .filter(Boolean)
      .map((n) => n[0])
      .join('')
      .toUpperCase()
      .slice(0, 2);
  }
  return email ? email[0].toUpperCase() : '?';
}

/**
 * Formats a duration in seconds for video player UI (HH:MM:SS or MM:SS format).
 * This is kept for video player controls and live timestamp display.
 * @param {number} totalSeconds - The duration in seconds.
 * @returns {string} The formatted duration string.
 */
export function formatDuration(totalSeconds: number): string {
  if (isNaN(totalSeconds) || totalSeconds < 0) {
    return '00:00';
  }

  const hours = Math.floor(totalSeconds / 3600);
  const minutes = Math.floor((totalSeconds % 3600) / 60);
  const seconds = Math.floor(totalSeconds % 60);

  const paddedMinutes = String(minutes).padStart(2, '0');
  const paddedSeconds = String(seconds).padStart(2, '0');

  if (hours > 0) {
    const paddedHours = String(hours).padStart(2, '0');
    return `${paddedHours}:${paddedMinutes}:${paddedSeconds}`;
  } else {
    return `${paddedMinutes}:${paddedSeconds}`;
  }
}

/**
 * Splits a duration in seconds into time parts, clamping invalid input to zero.
 * Shared internal helper for the clock/timestamp formatters below.
 */
function splitTime(totalSeconds: number): {
  hours: number;
  minutes: number;
  seconds: number;
  millis: number;
} {
  const s = isNaN(totalSeconds) || totalSeconds < 0 ? 0 : totalSeconds;
  return {
    hours: Math.floor(s / 3600),
    minutes: Math.floor((s % 3600) / 60),
    seconds: Math.floor(s % 60),
    millis: Math.floor((s % 1) * 1000),
  };
}

/**
 * Unpadded-minute clock for inline transcript/comment/playback labels.
 * e.g. 5 -> "0:05", 65 -> "1:05", 3661 -> "1:01:01". Invalid, `null`, or
 * `undefined` input -> "0:00".
 * Use this where the UI shows a compact running time; use {@link formatDuration}
 * where 2-digit-padded minutes are expected (e.g. "01:05").
 */
export function formatClock(totalSeconds: number | null | undefined): string {
  const { hours, minutes, seconds } = splitTime(totalSeconds ?? NaN);
  const ss = String(seconds).padStart(2, '0');
  return hours > 0 ? `${hours}:${String(minutes).padStart(2, '0')}:${ss}` : `${minutes}:${ss}`;
}

/**
 * Compact `Xh Ym`/`Ym Zs`/`Zs` duration, the "how long did this take" idiom
 * (issue #753's notification duration chip; e.g. `125` -> `"2m 5s"`,
 * `45` -> `"45s"`, `3660` -> `"1h 1m"`). Unlike {@link formatDuration}/
 * {@link formatClock} this omits zero units entirely rather than padding —
 * intentionally different, per `lib/utils/CLAUDE.md`'s do-not-migrate note
 * for compact `Xh Ym` durations. `$stores/websocket`'s `formatEtaSeconds`
 * delegates its arithmetic here and keeps its own `undefined`-for-no-ETA
 * guard on top, so the two duration idioms share one implementation instead
 * of two independently-maintained copies of the same minute/hour rollover.
 */
export function formatCompactDuration(totalSeconds: number): string {
  const seconds = Math.round(isNaN(totalSeconds) || totalSeconds < 0 ? 0 : totalSeconds);
  if (seconds < 60) return `${seconds}s`;
  const minutes = Math.floor(seconds / 60);
  const secs = seconds % 60;
  if (minutes < 60) return secs > 0 ? `${minutes}m ${secs}s` : `${minutes}m`;
  const hours = Math.floor(minutes / 60);
  const mins = minutes % 60;
  return mins > 0 ? `${hours}h ${mins}m` : `${hours}h`;
}

/**
 * Live player time with decimal milliseconds (padded minutes).
 * e.g. 3.25 -> "00:03.250", 3661.5 -> "01:01:01.500". Invalid input -> "00:00.000".
 */
export function formatTimeWithMillis(totalSeconds: number): string {
  const { hours, minutes, seconds, millis } = splitTime(totalSeconds);
  const body = `${String(minutes).padStart(2, '0')}:${String(seconds).padStart(2, '0')}.${String(
    millis
  ).padStart(3, '0')}`;
  return hours > 0 ? `${String(hours).padStart(2, '0')}:${body}` : body;
}

/**
 * SRT cue timestamp: "HH:MM:SS,mmm" (comma separator). Always emits full padded
 * hours, which SRT requires even under one hour. Invalid input -> "00:00:00,000".
 */
export function formatSrtTimestamp(totalSeconds: number): string {
  const { hours, minutes, seconds, millis } = splitTime(totalSeconds);
  return (
    `${String(hours).padStart(2, '0')}:${String(minutes).padStart(2, '0')}:` +
    `${String(seconds).padStart(2, '0')},${String(millis).padStart(3, '0')}`
  );
}

/**
 * WebVTT cue timestamp: "HH:MM:SS.mmm" (dot separator). Always emits full padded
 * hours, which VTT requires. Invalid input -> "00:00:00.000".
 */
export function formatVttTimestamp(totalSeconds: number): string {
  return formatSrtTimestamp(totalSeconds).replace(',', '.');
}

/**
 * Task progress (a backend `float` in 0..1) as an integer percentage 0-100.
 *
 * Guards three real failure modes in `TasksGrid.svelte` / `FileDetailModal.svelte`,
 * which each rendered `task.progress * 100` inline from an `any`-typed prop:
 *
 *  - **Missing/`null`/`NaN` → 0**, not `NaN`. `TasksGrid` had no guard at all, so a
 *    task row without `progress` rendered `style="width: NaN%"` (an invalid
 *    declaration the browser drops, leaving a zero-width bar and `NaN%` text).
 *    `FileDetailModal` guarded `!== undefined`, which `null` PASSES, so a nullable
 *    `progress` would have printed `Math.round(null * 100)` = "0%" — a plausible
 *    value, which is worse than a visibly broken one.
 *  - **Out-of-range → clamped.** The backend reported a hardcoded `0.5` for eleven
 *    months before it started reporting truthfully; if it ever switches to a 0-100
 *    scale, an unclamped `* 100` renders a 5000%-wide bar and no test fails.
 *    Clamping bounds the damage to "pinned at 100%", which is visible and safe.
 *
 * Deliberately does NOT try to auto-detect the scale: silently reinterpreting
 * `1` as either "1%" or "100%" would hide a contract change instead of surfacing it.
 */
export function taskProgressPercent(progress: unknown): number {
  const value = typeof progress === 'number' ? progress : Number.NaN;
  if (!Number.isFinite(value)) return 0;
  return Math.min(100, Math.max(0, Math.round(value * 100)));
}

/**
 * Translation key + plural count for a rate-limit "try again in..." hint
 * (issue #788). Returns a key rather than a rendered string so the caller
 * supplies the actual translation via `$t(key, { count })` — this file stays
 * i18n-agnostic like every other formatter here, and i18next's `_one`/`_other`
 * plural suffixes need the real `count` value at call time, not baked in here.
 *
 * Deliberately STATIC, not a live countdown (issue #788 J7): a ticking label
 * would re-announce on every assistive-tech pass since the toast/error region
 * is `role="alert"`/`role="status"`. One number, chosen once, is both simpler
 * and more accessible than a per-second re-render.
 *
 * @param seconds Seconds to wait, as parsed from a `Retry-After` header.
 *   Non-finite or non-positive input is clamped to 1 second — "try again in a
 *   moment" is safer than a nonsensical "in -3 seconds" or "in NaN seconds".
 */
export function retryWaitLabel(seconds: number): { key: string; count: number } {
  const clamped = Number.isFinite(seconds) ? Math.max(1, Math.round(seconds)) : 1;
  if (clamped < 60) {
    return { key: 'common.retryAfterSeconds', count: clamped };
  }
  const minutes = Math.max(1, Math.round(clamped / 60));
  return { key: 'common.retryAfterMinutes', count: minutes };
}

/**
 * Render ISO language codes as names in the reader's own language.
 *
 * Purely presentational, and one of the approved client-side transforms: it is
 * `Intl.*` locale formatting over data the backend already sent, not a business
 * rule. The backend deliberately sends codes rather than names — a name is a
 * display choice that depends on who is reading.
 *
 * Falls back to the raw code for anything `Intl.DisplayNames` does not know, and
 * for the whole call if the runtime lacks it, so an unrecognised code degrades to
 * "es" rather than disappearing. A language silently dropped from this list would
 * understate the warning it appears in.
 *
 * @param codes ISO 639-1 codes, e.g. `['es', 'fr']`.
 * @param locale BCP-47 locale to render the names in; defaults to the browser's.
 * @returns A localized, comma-joined list, or `''` when `codes` is empty.
 */
export function formatLanguageNames(codes: string[], locale?: string): string {
  if (!codes.length) return '';

  let display: Intl.DisplayNames | null = null;
  try {
    display = new Intl.DisplayNames([locale ?? navigator.language], { type: 'language' });
  } catch {
    display = null;
  }

  const names = codes.map((code) => {
    if (!display) return code;
    try {
      return display.of(code) ?? code;
    } catch {
      return code;
    }
  });

  return names.join(', ');
}
