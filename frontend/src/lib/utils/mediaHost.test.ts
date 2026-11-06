import { describe, it, expect } from 'vitest';
import { hostIdentity } from '$lib/utils/mediaHost';

describe('hostIdentity', () => {
  it('lower-cases the hostname and fills in the default port for the protocol', () => {
    expect(hostIdentity('Media.Example.com', 'https:')).toBe('media.example.com:443');
    expect(hostIdentity('media.example.com', 'http:')).toBe('media.example.com:80');
  });

  it('keeps an explicit port', () => {
    expect(hostIdentity('media.example.com:8443', 'https:')).toBe('media.example.com:8443');
  });

  it('ignores surrounding whitespace', () => {
    expect(hostIdentity('  media.example.com  ', 'https:')).toBe('media.example.com:443');
  });

  it.each([
    ['userinfo', 'user@media.example.com'],
    ['a backslash', 'media.example.com\\evil'],
    ['a percent-escape', 'media%2eexample.com'],
    ['an internal space', 'media example.com'],
    ['a non-ASCII hostname', 'bücher.example'],
    ['an empty value', ''],
  ])('refuses %s', (_label, raw) => {
    expect(hostIdentity(raw, 'https:')).toBeNull();
  });
});
