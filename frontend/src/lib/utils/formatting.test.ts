import { describe, it, expect } from 'vitest';
import {
  formatDuration,
  getInitials,
  formatClock,
  formatTimeWithMillis,
  formatSrtTimestamp,
  formatVttTimestamp,
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
