import { afterEach, describe, expect, it, vi } from 'vitest';
import { getFlowerUrl } from '$lib/utils/url';

const ORIGIN = 'https://transcribe.example:8443';

function stubPage(origin: string = ORIGIN): void {
  vi.stubGlobal('window', { location: { origin } });
}

describe('getFlowerUrl', () => {
  afterEach(() => {
    vi.unstubAllGlobals();
    vi.unstubAllEnvs();
  });

  it('returns an empty string when there is no window (SSR)', () => {
    vi.stubGlobal('window', undefined);
    expect(getFlowerUrl()).toBe('');
  });

  it('defaults to /flower/ on the page origin when the prefix is unset', () => {
    stubPage();
    vi.stubEnv('VITE_FLOWER_URL_PREFIX', undefined);
    expect(getFlowerUrl()).toBe(`${ORIGIN}/flower/`);
  });

  it('defaults to /flower/ when the prefix is empty', () => {
    stubPage();
    vi.stubEnv('VITE_FLOWER_URL_PREFIX', '');
    expect(getFlowerUrl()).toBe(`${ORIGIN}/flower/`);
  });

  it.each([
    ['/flower/', `${ORIGIN}/flower/`],
    ['flower', `${ORIGIN}/flower/`],
    ['//x//', `${ORIGIN}/x/`],
    ['ops/flower', `${ORIGIN}/ops/flower/`],
    ['/ops/flower/', `${ORIGIN}/ops/flower/`],
  ])('normalises the prefix %j', (prefix, expected) => {
    stubPage();
    vi.stubEnv('VITE_FLOWER_URL_PREFIX', prefix);
    expect(getFlowerUrl()).toBe(expected);
  });

  it('always builds on the page origin, never another host', () => {
    stubPage('http://10.0.0.5:5173');
    vi.stubEnv('VITE_FLOWER_URL_PREFIX', 'flower');
    expect(getFlowerUrl()).toBe('http://10.0.0.5:5173/flower/');
  });
});
