import { describe, it, expect } from 'vitest';
import {
  formatDuration,
  getInitials,
  formatClock,
  formatTimeWithMillis,
  formatSrtTimestamp,
  formatVttTimestamp,
  formatLanguageNames,
} from './formatting';

/**
 * Characterization tests — lock the CURRENT behavior of the shared formatters
 * before the Phase 1 consolidation migrates ~25 local copies onto them.
 * New functions (formatClock / formatTimeWithMillis / formatSrtTimestamp /
 * formatVttTimestamp) get their expectations added in Phase 1.1.
 */
describe('formatDuration (padded MM:SS / HH:MM:SS)', () => {
  it('formats sub-hour durations as MM:SS', () => {
    expect(formatDuration(0)).toBe('00:00');
    expect(formatDuration(5)).toBe('00:05');
    expect(formatDuration(65)).toBe('01:05');
    expect(formatDuration(599)).toBe('09:59');
  });

  it('formats hour+ durations as HH:MM:SS', () => {
    expect(formatDuration(3600)).toBe('01:00:00');
    expect(formatDuration(3661)).toBe('01:01:01');
    expect(formatDuration(36000)).toBe('10:00:00');
  });

  it('clamps invalid input to 00:00', () => {
    expect(formatDuration(-1)).toBe('00:00');
    expect(formatDuration(Number.NaN)).toBe('00:00');
  });
});

describe('getInitials', () => {
  it('derives 1-2 initials from a name', () => {
    expect(getInitials('David Macey', 'x@y.com')).toBe('DM');
    expect(getInitials('Madonna', 'x@y.com')).toBe('M');
    expect(getInitials('a b c d', 'x@y.com')).toBe('AB');
  });

  it('falls back to the email initial when no name', () => {
    expect(getInitials(null, 'admin@example.com')).toBe('A');
    expect(getInitials('', 'zed@example.com')).toBe('Z');
  });

  it('returns ? when neither is usable', () => {
    expect(getInitials(null, '')).toBe('?');
  });
});

describe('formatClock (unpadded minutes)', () => {
  it('formats sub-hour as M:SS', () => {
    expect(formatClock(0)).toBe('0:00');
    expect(formatClock(5)).toBe('0:05');
    expect(formatClock(65)).toBe('1:05');
    expect(formatClock(599)).toBe('9:59');
  });
  it('formats hour+ as H:MM:SS', () => {
    expect(formatClock(3661)).toBe('1:01:01');
  });
  it('clamps invalid input to 0:00', () => {
    expect(formatClock(-1)).toBe('0:00');
    expect(formatClock(Number.NaN)).toBe('0:00');
  });

  // Moved from chatMarkdown.test.ts when chatMarkdown.ts's duplicate
  // formatClock (which used `seconds ?? 0` and so did NOT catch NaN) was
  // deleted in favor of re-exporting this implementation.
  it('clamps null and undefined input to 0:00', () => {
    expect(formatClock(null)).toBe('0:00');
    expect(formatClock(undefined)).toBe('0:00');
  });
});

describe('formatTimeWithMillis (decimal ms)', () => {
  it('formats sub-hour as MM:SS.mmm', () => {
    expect(formatTimeWithMillis(3.25)).toBe('00:03.250');
    expect(formatTimeWithMillis(0)).toBe('00:00.000');
  });
  it('formats hour+ as HH:MM:SS.mmm', () => {
    expect(formatTimeWithMillis(3661.5)).toBe('01:01:01.500');
  });
});

describe('formatSrtTimestamp / formatVttTimestamp (always full HH)', () => {
  it('SRT uses a comma and full padded hours', () => {
    expect(formatSrtTimestamp(0)).toBe('00:00:00,000');
    expect(formatSrtTimestamp(3661.25)).toBe('01:01:01,250');
    expect(formatSrtTimestamp(5.5)).toBe('00:00:05,500');
  });
  it('VTT mirrors SRT with a dot separator', () => {
    expect(formatVttTimestamp(3661.25)).toBe('01:01:01.250');
    expect(formatVttTimestamp(5.5)).toBe('00:00:05.500');
  });
  it('clamps invalid input to zero', () => {
    expect(formatSrtTimestamp(Number.NaN)).toBe('00:00:00,000');
    expect(formatVttTimestamp(-3)).toBe('00:00:00.000');
  });
});

describe('formatLanguageNames', () => {
  it('renders ISO codes as human language names', () => {
    // The backend deliberately sends codes; the name is a display choice. A
    // warning reading "recordings are in es, fr" is markedly worse than one
    // naming Spanish and French, and this is the only place that conversion
    // happens (the component test cannot see it — vitest loads no locale bundle).
    expect(formatLanguageNames(['es', 'fr'], 'en')).toBe('Spanish, French');
  });

  it('renders the names in the READER’s language, not the content’s', () => {
    // Intl.DisplayNames takes the display locale as its first argument. Getting
    // this backwards would name Spanish "español" for an English reader —
    // plausible-looking output that is wrong for everyone.
    expect(formatLanguageNames(['es'], 'fr')).toBe('espagnol');
  });

  it('falls back to the raw code for a language it does not know', () => {
    // A dropped language would UNDERSTATE the warning it appears in, which is
    // worse than an ugly one: the user would be told fewer of their recordings
    // were unsupported than actually were.
    expect(formatLanguageNames(['zz'], 'en')).toBe('zz');
  });

  it('returns an empty string for no languages', () => {
    expect(formatLanguageNames([], 'en')).toBe('');
  });
});
