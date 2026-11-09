import { describe, expect, it } from 'vitest';
import { startupRedirect, type StartupRedirectInput } from './startupRedirect';

const base: StartupRedirectInput = {
  isAuthenticated: false,
  path: '/',
  isCloudEdition: false,
  publicPaths: ['/login', '/register', '/accept-invite'],
  localCredentialPaths: ['/register', '/forgot-password'],
};

describe('startupRedirect', () => {
  it.each([
    ['an anonymous visitor on a private page goes to login', { path: '/files/1' }, '/login'],
    ['an anonymous visitor on a public page stays', { path: '/login' }, null],
    [
      'a signed-in visitor on a private page stays',
      { isAuthenticated: true, path: '/files/1' },
      null,
    ],
    [
      'a signed-in visitor on a public page goes home',
      { isAuthenticated: true, path: '/login' },
      '/',
    ],
  ] as [string, Partial<StartupRedirectInput>, string | null][])('%s', (_name, over, expected) => {
    expect(startupRedirect({ ...base, ...over })).toBe(expected);
  });

  it('sends a cloud visitor off a local credential route to login, signed in or not', () => {
    const cloud = { ...base, isCloudEdition: true, path: '/register' };
    expect(startupRedirect(cloud)).toBe('/login');
    expect(startupRedirect({ ...cloud, isAuthenticated: true })).toBe('/login');
  });

  it('leaves the same route alone on the community edition', () => {
    expect(startupRedirect({ ...base, path: '/register' })).toBeNull();
  });
});
