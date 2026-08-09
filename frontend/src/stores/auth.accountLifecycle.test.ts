/**
 * The two account-lifecycle 403s carry an OBJECT `detail`, unlike every other
 * error in the app. These tests pin that the classifier keys off `detail.code`
 * and nothing else — no status-only shortcut, no message string-matching, and
 * no swallowing of an ordinary 403.
 */
import { describe, it, expect, vi, beforeEach } from 'vitest';
import { get } from 'svelte/store';

vi.mock('$lib/axios', () => ({
  default: { get: vi.fn(), post: vi.fn(), put: vi.fn() },
  abortAllRequests: vi.fn(),
}));

vi.mock('$stores/locale', () => ({
  t: {
    subscribe: (run: (value: (key: string) => string) => void) => {
      run((key: string) => key);
      return () => {};
    },
  },
}));

vi.mock('$lib/session/clearUserState', () => ({ clearUserState: vi.fn() }));
vi.mock('$lib/edition', () => ({ isCloudEdition: false }));

import axiosInstance from '$lib/axios';
import {
  readAccountLifecycle,
  handleAccountLifecycleError,
  clearAccountLifecycle,
  accountLifecycle,
  changeOwnPassword,
  isAuthenticated,
  authStore,
} from './auth';

const forbidden = (detail: unknown) => ({ response: { status: 403, data: { detail } } });

describe('readAccountLifecycle', () => {
  beforeEach(() => {
    vi.clearAllMocks();
    clearAccountLifecycle();
  });

  it('recognises password_change_required from the object detail', () => {
    const state = readAccountLifecycle(
      forbidden({ code: 'password_change_required', message: 'You must change your password.' })
    );

    expect(state).toEqual({
      code: 'password_change_required',
      message: 'You must change your password.',
    });
  });

  it('recognises account_expired from the object detail', () => {
    const state = readAccountLifecycle(
      forbidden({ code: 'account_expired', message: 'This account expired on 2026-01-01.' })
    );

    expect(state?.code).toBe('account_expired');
  });

  it('branches on the code, not on the prose', () => {
    // Same English wording the backend happens to use today, but no code: this
    // must NOT be treated as a lifecycle refusal. A client that string-matches
    // breaks on the first rewording or translation.
    expect(
      readAccountLifecycle(forbidden('You must change your password before continuing.'))
    ).toBeNull();

    // And a recognised code wins regardless of what the message says.
    expect(readAccountLifecycle(forbidden({ code: 'account_expired', message: '' }))?.code).toBe(
      'account_expired'
    );
  });

  it('ignores an ordinary permission 403 with a string detail', () => {
    expect(readAccountLifecycle(forbidden('Not enough permissions'))).toBeNull();
  });

  it('ignores an array detail (Pydantic validation errors)', () => {
    expect(readAccountLifecycle(forbidden([{ msg: 'field required' }]))).toBeNull();
  });

  it('ignores an unrecognised code so new server states fail closed to normal errors', () => {
    expect(readAccountLifecycle(forbidden({ code: 'something_new', message: 'x' }))).toBeNull();
  });

  it('ignores non-403 statuses even with a matching code', () => {
    expect(
      readAccountLifecycle({
        response: { status: 401, data: { detail: { code: 'account_expired', message: 'x' } } },
      })
    ).toBeNull();
  });

  it('ignores errors with no response at all (network failure)', () => {
    expect(readAccountLifecycle(new Error('Network Error'))).toBeNull();
    expect(readAccountLifecycle(undefined)).toBeNull();
  });

  it('tolerates a missing message rather than throwing', () => {
    expect(readAccountLifecycle(forbidden({ code: 'account_expired' }))).toEqual({
      code: 'account_expired',
      message: '',
    });
  });
});

describe('handleAccountLifecycleError', () => {
  beforeEach(() => {
    vi.clearAllMocks();
    clearAccountLifecycle();
    authStore.setToken('cookie');
  });

  it('publishes the hold and KEEPS the session for a forced password change', async () => {
    const state = await handleAccountLifecycleError(
      forbidden({ code: 'password_change_required', message: 'change it' })
    );

    expect(state?.code).toBe('password_change_required');
    expect(get(accountLifecycle)?.code).toBe('password_change_required');
    // PUT /users/me is what clears the flag and it needs this session — tearing
    // it down here would make the remedy unreachable.
    expect(get(isAuthenticated)).toBe(true);
    expect(vi.mocked(axiosInstance.post)).not.toHaveBeenCalled();
  });

  it('logs out for an expired account but keeps the reason visible', async () => {
    vi.mocked(axiosInstance.post).mockResolvedValue({} as never);

    await handleAccountLifecycleError(
      forbidden({ code: 'account_expired', message: 'expired on 2026-01-01' })
    );

    expect(vi.mocked(axiosInstance.post)).toHaveBeenCalledWith('/auth/logout');
    expect(get(isAuthenticated)).toBe(false);
    // logout() clears the hold; it is re-published so the login page can explain
    // why the session ended instead of showing a bare sign-in form.
    expect(get(accountLifecycle)).toEqual({
      code: 'account_expired',
      message: 'expired on 2026-01-01',
    });
  });

  it('does nothing for an unrelated error', async () => {
    expect(await handleAccountLifecycleError(forbidden('Not enough permissions'))).toBeNull();
    expect(get(accountLifecycle)).toBeNull();
  });
});

describe('changeOwnPassword', () => {
  beforeEach(() => {
    vi.clearAllMocks();
    clearAccountLifecycle();
  });

  it('posts both passwords in the body and releases the hold on success', async () => {
    vi.mocked(axiosInstance.put).mockResolvedValue({ data: {} } as never);
    vi.mocked(axiosInstance.get).mockResolvedValue({ data: { uuid: 'u1' } } as never);
    accountLifecycle.set({ code: 'password_change_required', message: 'change it' });

    const result = await changeOwnPassword('old-secret', 'new-secret');

    expect(result.success).toBe(true);
    expect(vi.mocked(axiosInstance.put)).toHaveBeenCalledWith('/users/me', {
      current_password: 'old-secret',
      password: 'new-secret',
    });
    expect(get(accountLifecycle)).toBeNull();
  });

  it("surfaces the backend's policy message and keeps the hold", async () => {
    accountLifecycle.set({ code: 'password_change_required', message: 'change it' });
    vi.mocked(axiosInstance.put).mockRejectedValue({
      response: { status: 400, data: { detail: 'Password has been used recently.' } },
    });

    const result = await changeOwnPassword('old-secret', 'weak');

    expect(result).toEqual({ success: false, message: 'Password has been used recently.' });
    expect(get(accountLifecycle)?.code).toBe('password_change_required');
  });
});
