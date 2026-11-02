import { describe, it, expect, vi, beforeEach } from 'vitest';
import { writable } from 'svelte/store';

const handle = vi.hoisted(() => vi.fn());
const authed = vi.hoisted(() => ({ value: null as unknown }));
vi.mock('$stores/auth', async () => {
  const { writable: w } = await import('svelte/store');
  authed.value = w(false);
  return { handleOIDCCallback: handle, isAuthenticated: authed.value };
});

import { runOidcCallback } from './oidcCallback';

beforeEach(() => {
  vi.clearAllMocks();
  sessionStorage.clear();
  (authed.value as ReturnType<typeof writable<boolean>>).set(false);
});

describe('runOidcCallback', () => {
  it('succeeds and toggles loading around the exchange', async () => {
    handle.mockResolvedValue({ success: true });
    const loading = vi.fn();
    expect(await runOidcCallback('c', 's', loading)).toEqual({ kind: 'success' });
    expect(loading.mock.calls).toEqual([[true], [false]]);
    expect(handle).toHaveBeenCalledWith('c', 's');
  });

  it('treats a replayed callback as already handled, without calling the API', async () => {
    sessionStorage.setItem('oidc_callback_s', 'true');
    expect(await runOidcCallback('c', 's', vi.fn())).toEqual({ kind: 'redirect-home' });
    expect(handle).not.toHaveBeenCalled();
  });

  it('reports a non-state failure as an error with the server message', async () => {
    handle.mockResolvedValue({ success: false, message: 'denied' });
    expect(await runOidcCallback('c', 's', vi.fn())).toEqual({ kind: 'error', message: 'denied' });
  });

  it('a state failure goes home only if the user is in fact signed in', async () => {
    handle.mockResolvedValue({ success: false, message: 'invalid state' });
    expect(await runOidcCallback('c', 's', vi.fn())).toEqual({
      kind: 'error',
      message: 'invalid state',
    });
    sessionStorage.clear();
    (authed.value as ReturnType<typeof writable<boolean>>).set(true);
    expect(await runOidcCallback('c', 's', vi.fn())).toEqual({ kind: 'redirect-home' });
  });

  it('never throws: blocked storage becomes an exception outcome', async () => {
    const spy = vi.spyOn(Storage.prototype, 'getItem').mockImplementation(() => {
      throw new Error('SecurityError');
    });
    const err = vi.spyOn(console, 'error').mockImplementation(() => {});
    const loading = vi.fn();
    expect(await runOidcCallback('c', 's', loading)).toEqual({ kind: 'exception' });
    expect(loading).toHaveBeenLastCalledWith(false);
    spy.mockRestore();
    err.mockRestore();
  });
});
