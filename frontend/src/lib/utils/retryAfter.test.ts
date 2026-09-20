import { describe, it, expect, vi, afterEach } from 'vitest';
import { parseRetryAfter } from './retryAfter';

describe('parseRetryAfter (RFC 9110 delta-seconds or HTTP-date)', () => {
  afterEach(() => {
    vi.useRealTimers();
  });

  it('returns null for a missing header', () => {
    expect(parseRetryAfter(null)).toBeNull();
    expect(parseRetryAfter(undefined)).toBeNull();
  });

  it('returns null for an empty string', () => {
    expect(parseRetryAfter('')).toBeNull();
    expect(parseRetryAfter('   ')).toBeNull();
  });

  it('parses "0" as zero seconds, not null', () => {
    // A bucket that already reset is a valid ("wait nothing") answer, distinct
    // from "unparseable" -- the caller decides whether to show a hint at all.
    expect(parseRetryAfter('0')).toBe(0);
  });

  it('rejects a negative delta-seconds value (RFC 9110 forbids it)', () => {
    expect(parseRetryAfter('-5')).toBeNull();
  });

  it('rejects a non-integer string', () => {
    expect(parseRetryAfter('abc')).toBeNull();
  });

  it('rejects a fractional delta-seconds value', () => {
    // RFC 9110 delta-seconds is an integer; "3.7" is not a valid HTTP-date
    // either, so this must degrade to null rather than guessing.
    expect(parseRetryAfter('3.7')).toBeNull();
  });

  it('parses a plain positive integer as delta-seconds', () => {
    expect(parseRetryAfter('120')).toBe(120);
  });

  it('parses a future HTTP-date into a positive delta', () => {
    vi.useFakeTimers();
    vi.setSystemTime(new Date('2026-01-01T00:00:00Z'));

    // 30 seconds in the future, in the RFC 9110-preferred IMF-fixdate format.
    expect(parseRetryAfter('Thu, 01 Jan 2026 00:00:30 GMT')).toBe(30);
  });

  it('returns null for a PAST HTTP-date -- nothing left to wait for', () => {
    vi.useFakeTimers();
    vi.setSystemTime(new Date('2026-01-01T00:01:00Z'));

    expect(parseRetryAfter('Thu, 01 Jan 2026 00:00:00 GMT')).toBeNull();
  });
});
