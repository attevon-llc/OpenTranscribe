import { describe, it, expect } from 'vitest';
import {
  hostIdentity,
  isPlainHttpSource,
  isPrivateHostRefusal,
  sourceOrigin,
} from '$lib/utils/mediaHost';

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
    ['a path', 'media.example.com/x'],
    ['an empty value', ''],
  ])('refuses %s', (_label, raw) => {
    expect(hostIdentity(raw, 'https:')).toBeNull();
  });
});

describe('sourceOrigin', () => {
  it.each([
    ['media.example.com', 'https://media.example.com:443'],
    ['https://Media.Example.com:8443', 'https://media.example.com:8443'],
    ['http://media.example.com', 'http://media.example.com:80'],
    ['HTTP://media.example.com:8080', 'http://media.example.com:8080'],
  ])('reads %s as %s (no scheme means https)', (configured, origin) => {
    expect(sourceOrigin(configured)).toBe(origin);
  });

  it.each(['ftp://media.example.com', 'https://', 'https://media.example.com/x'])(
    'refuses %s',
    (configured) => {
      expect(sourceOrigin(configured)).toBeNull();
    }
  );
});

describe('isPlainHttpSource', () => {
  it('flags only an explicit http:// address', () => {
    expect(isPlainHttpSource('http://media.example.com')).toBe(true);
    expect(isPlainHttpSource(' HTTP://media.example.com')).toBe(true);
    expect(isPlainHttpSource('https://media.example.com')).toBe(false);
    expect(isPlainHttpSource('media.example.com')).toBe(false);
  });
});

describe('isPrivateHostRefusal', () => {
  const refusal = {
    response: {
      data: {
        detail: [
          { type: 'value_error', msg: 'Value error, media_source_private_host: not reachable' },
        ],
      },
    },
  };

  it('recognises the stable code inside a 422 validation detail', () => {
    expect(isPrivateHostRefusal(refusal)).toBe(true);
  });

  it('ignores other validation errors and non-API errors', () => {
    expect(
      isPrivateHostRefusal({
        response: { data: { detail: [{ msg: 'Value error, Invalid port number' }] } },
      })
    ).toBe(false);
    expect(isPrivateHostRefusal(new Error('network'))).toBe(false);
    expect(isPrivateHostRefusal(undefined)).toBe(false);
  });
});
