import { describe, expect, it, vi } from 'vitest';
import { runStartup } from './startup';

function deferred<T = void>() {
  let resolve!: (v: T) => void;
  let reject!: (e: unknown) => void;
  const promise = new Promise<T>((res, rej) => {
    resolve = res;
    reject = rej;
  });
  return { promise, resolve, reject };
}

describe('runStartup', () => {
  it('starts locale, auth methods and auth init before any of them settles', async () => {
    const locale = deferred();
    const methods = deferred<{ login_banner_enabled: boolean }>();
    const auth = deferred();
    const initLocale = vi.fn(() => locale.promise);
    const getAuthMethods = vi.fn(() => methods.promise);
    const initAuth = vi.fn(() => auth.promise);

    const done = runStartup({ initLocale, getAuthMethods, initAuth });

    // All three are in flight while none has resolved: they are concurrent, not chained.
    expect(initLocale).toHaveBeenCalledTimes(1);
    expect(getAuthMethods).toHaveBeenCalledTimes(1);
    expect(initAuth).toHaveBeenCalledTimes(1);

    methods.resolve({ login_banner_enabled: true });
    locale.resolve();
    auth.resolve();
    await expect(done).resolves.toEqual({
      authMethods: { login_banner_enabled: true },
      authFailed: false,
      authError: null,
    });
  });

  it('keeps going when auth methods fail, losing only the methods', async () => {
    const warn = vi.spyOn(console, 'warn').mockImplementation(() => {});
    const result = await runStartup({
      initLocale: async () => {},
      getAuthMethods: async () => {
        throw new Error('429');
      },
      initAuth: async () => {},
    });
    expect(result.authMethods).toBeNull();
    expect(result.authFailed).toBe(false);
    warn.mockRestore();
  });

  it('reports an auth failure so the caller can redirect', async () => {
    const boom = new Error('network');
    const result = await runStartup({
      initLocale: async () => {},
      getAuthMethods: async () => ({}),
      initAuth: async () => {
        throw boom;
      },
    });
    expect(result.authFailed).toBe(true);
    expect(result.authError).toBe(boom);
  });

  it('does not let a locale failure block auth', async () => {
    const warn = vi.spyOn(console, 'warn').mockImplementation(() => {});
    const result = await runStartup({
      initLocale: async () => {
        throw new Error('bundle');
      },
      getAuthMethods: async () => ({ x: 1 }),
      initAuth: async () => {},
    });
    expect(result.authFailed).toBe(false);
    expect(result.authMethods).toEqual({ x: 1 });
    warn.mockRestore();
  });
});
