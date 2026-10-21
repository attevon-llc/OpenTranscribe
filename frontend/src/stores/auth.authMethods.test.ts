import { describe, it, expect, vi, beforeEach } from 'vitest';

vi.mock('$lib/axios', () => ({
  default: { get: vi.fn(), post: vi.fn() },
  abortAllRequests: vi.fn(),
}));

vi.mock('$stores/locale', () => ({
  // Identity translator: assertions can key off the i18n key itself, which also
  // proves the message went through i18n rather than a hardcoded English string.
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
import { getAuthMethods, invalidateAuthMethods, login } from './auth';

const mockedGet = vi.mocked(axiosInstance.get);
const mockedPost = vi.mocked(axiosInstance.post);

describe('getAuthMethods', () => {
  beforeEach(() => {
    vi.clearAllMocks();
    invalidateAuthMethods();
  });

  describe('shared between the layout and the login page', () => {
    it('concurrent callers share one request', async () => {
      mockedGet.mockResolvedValue({ data: { methods: ['local'] } } as never);

      const [a, b] = await Promise.all([getAuthMethods(), getAuthMethods()]);

      expect(mockedGet).toHaveBeenCalledTimes(1);
      expect(a).toBe(b);
    });

    it('a caller right after the first settled gets the same answer without a request', async () => {
      mockedGet.mockResolvedValue({ data: { methods: ['local'] } } as never);

      const boot = await getAuthMethods(); // the layout's boot call
      const loginPage = await getAuthMethods(); // the login page mounting a moment later

      expect(mockedGet).toHaveBeenCalledTimes(1);
      expect(loginPage).toEqual(boot);
      expect(loginPage.methods).toEqual(['local']);
    });

    it('does not remember a failure: the next caller asks the server again', async () => {
      vi.spyOn(console, 'error').mockImplementation(() => {});
      mockedGet.mockRejectedValueOnce(new Error('down'));
      mockedGet.mockResolvedValueOnce({ data: { methods: ['oidc'] } } as never);

      const failed = await getAuthMethods();
      const recovered = await getAuthMethods();

      expect(failed.methods).toEqual(['local']);
      expect(recovered.methods).toEqual(['oidc']);
      expect(mockedGet).toHaveBeenCalledTimes(2);
    });

    it('invalidateAuthMethods() forces a fresh request (after an admin edits the config)', async () => {
      mockedGet.mockResolvedValueOnce({ data: { methods: ['local'] } } as never);
      mockedGet.mockResolvedValueOnce({ data: { methods: ['oidc'] } } as never);

      await getAuthMethods();
      invalidateAuthMethods();
      const afterEdit = await getAuthMethods();

      expect(mockedGet).toHaveBeenCalledTimes(2);
      expect(afterEdit.methods).toEqual(['oidc']);
    });

    it('expires after the freshness window', async () => {
      vi.useFakeTimers();
      try {
        mockedGet.mockResolvedValueOnce({ data: { methods: ['local'] } } as never);
        mockedGet.mockResolvedValueOnce({ data: { methods: ['oidc'] } } as never);

        await getAuthMethods();
        vi.advanceTimersByTime(60_000);
        const later = await getAuthMethods();

        expect(mockedGet).toHaveBeenCalledTimes(2);
        expect(later.methods).toEqual(['oidc']);
      } finally {
        vi.useRealTimers();
      }
    });
  });

  it('passes the backend flags through untouched', async () => {
    mockedGet.mockResolvedValueOnce({
      data: {
        methods: ['ldap', 'oidc'],
        oidc_enabled: true,
        pki_enabled: false,
        ldap_enabled: true,
        local_enabled: false,
        allow_registration: false,
        mfa_enabled: true,
        mfa_required: false,
        login_banner_enabled: false,
        login_banner_text: '',
        login_banner_classification: 'UNCLASSIFIED',
      },
    } as never);

    const methods = await getAuthMethods();

    expect(methods.local_enabled).toBe(false);
    expect(methods.allow_registration).toBe(false);
    expect(methods.methods).not.toContain('local');
  });

  it('fails closed asymmetrically when the probe fails', async () => {
    mockedGet.mockRejectedValueOnce(new Error('network down'));

    const methods = await getAuthMethods();

    // Still reachable: a transient probe failure must not hide the only sign-in
    // form a self-hosted operator has.
    expect(methods.local_enabled).toBe(true);
    // Never advertised: a signup link we cannot confirm either 403s or exposes
    // registration the operator turned off.
    expect(methods.allow_registration).toBe(false);
    expect(methods.oidc_enabled).toBe(false);
    expect(methods.pki_enabled).toBe(false);
    expect(methods.ldap_enabled).toBe(false);
  });
});

describe('login error reporting', () => {
  beforeEach(() => {
    vi.clearAllMocks();
  });

  it('returns the HTTP status so callers need not parse localised text', async () => {
    mockedPost.mockRejectedValueOnce({
      response: { status: 401, data: { detail: 'Invalid credentials' } },
    });

    const result = await login('someone@example.com', 'wrong');

    expect(result.success).toBe(false);
    expect(result.status).toBe(401);
    expect(result.message).toBe('Invalid credentials');
  });

  it('surfaces a translated message when the backend sends no detail', async () => {
    mockedPost.mockRejectedValueOnce({ response: { status: 429, data: {} } });

    const result = await login('someone@example.com', 'wrong');

    expect(result.status).toBe(429);
    expect(result.message).toBe('auth.error.tooManyLoginAttempts');
  });
});
