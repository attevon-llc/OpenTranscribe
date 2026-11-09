import { describe, expect, it } from 'vitest';
import { describeUserAgent } from './userAgent';

describe('describeUserAgent', () => {
  it.each([
    [
      'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 Chrome/126.0 Safari/537.36 Edg/126.0',
      'Edge — Windows',
    ],
    [
      'Mozilla/5.0 (Windows NT 10.0) AppleWebKit/537.36 Chrome/126.0 Safari/537.36 OPR/110.0',
      'Opera — Windows',
    ],
    [
      'Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 Chrome/126.0 Safari/537.36',
      'Chrome — Linux',
    ],
    ['Mozilla/5.0 (X11; Linux x86_64; rv:127.0) Gecko/20100101 Firefox/127.0', 'Firefox — Linux'],
    [
      'Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/605.1.15 Version/17.5 Safari/605.1.15',
      'Safari — macOS',
    ],
  ])('names browser and OS for %s', (ua, expected) => {
    expect(describeUserAgent(ua)).toBe(expected);
  });

  it('reads an Android phone as Android, not Linux, and an iPhone as iOS, not macOS', () => {
    expect(
      describeUserAgent(
        'Mozilla/5.0 (Linux; Android 14; Pixel 8) AppleWebKit/537.36 Chrome/126.0 Mobile Safari/537.36'
      )
    ).toBe('Chrome — Android');
    expect(
      describeUserAgent(
        'Mozilla/5.0 (iPhone; CPU iPhone OS 17_5 like Mac OS X) AppleWebKit/605.1.15 Safari/604.1'
      )
    ).toBe('Safari — iOS');
  });

  it('falls back to what it can recognise, then to the raw string, and is null for nothing', () => {
    expect(describeUserAgent('curl/8.0 (Linux)')).toBe('Linux');
    expect(describeUserAgent('SomethingElse/1.0')).toBe('SomethingElse/1.0');
    expect(describeUserAgent(null)).toBeNull();
    expect(describeUserAgent('')).toBeNull();
  });
});
