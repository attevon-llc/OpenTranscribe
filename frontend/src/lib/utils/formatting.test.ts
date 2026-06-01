import { describe, it, expect } from 'vitest';
import { formatDuration, getInitials } from './formatting';

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
