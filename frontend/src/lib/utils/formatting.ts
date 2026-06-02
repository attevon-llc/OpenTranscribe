import { format } from 'date-fns';

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
 * Formats a timestamp for debugging or logging purposes.
 * @param {string | number | Date} timestamp - The timestamp to format.
 * @returns {string} The formatted timestamp string with milliseconds.
 */
export function formatTimestampWithMillis(timestamp: string | number | Date): string {
  try {
    const date = new Date(timestamp);
    if (isNaN(date.getTime())) {
      return 'Invalid Date';
    }
    const mainPart = format(date, 'yyyy-MM-dd HH:mm:ss');
    const milliseconds = String(date.getMilliseconds()).padStart(3, '0');
    return `${mainPart}.${milliseconds}`;
  } catch (error) {
    console.error('Error formatting timestamp:', error);
    return 'Invalid Date';
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
 * e.g. 5 -> "0:05", 65 -> "1:05", 3661 -> "1:01:01". Invalid input -> "0:00".
 * Use this where the UI shows a compact running time; use {@link formatDuration}
 * where 2-digit-padded minutes are expected (e.g. "01:05").
 */
export function formatClock(totalSeconds: number): string {
  const { hours, minutes, seconds } = splitTime(totalSeconds);
  const ss = String(seconds).padStart(2, '0');
  return hours > 0 ? `${hours}:${String(minutes).padStart(2, '0')}:${ss}` : `${minutes}:${ss}`;
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
