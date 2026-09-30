/**
 * External-auth edition: a 401 whose `detail.code` is `session_expired` (the server's
 * absolute-timeout backstop, issue #1106) must end the session through the timeout
 * path, which also ends the identity provider's session. The ordinary external-auth 401 path
 * only redirects to /login, where a still-live provider session signs straight back
 * in — so the refusal would loop forever.
 */
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';
import type { AxiosResponse, InternalAxiosRequestConfig } from 'axios';

vi.mock('$lib/edition', () => ({ isCloudEdition: true }));
vi.mock('$lib/cloud', () => ({
  getSessionToken: async () => 'external-token',
  showQuotaExceeded: () => {},
}));
const timeouts = vi.hoisted(() => ({ expired: [] as string[] }));
vi.mock('$lib/auth/sessionTimeouts', () => ({
  expireSessionNow: (reason: string) => timeouts.expired.push(reason),
}));

import axiosInstance from '$lib/axios';

let detail: unknown = null;
let locationHref = '';
const originalAdapter = axiosInstance.defaults.adapter;

function refuse(config: InternalAxiosRequestConfig): Promise<AxiosResponse> {
  const response = { status: 401, statusText: '401', data: { detail }, headers: {}, config };
  return Promise.reject(Object.assign(new Error('401'), { response, config, isAxiosError: true }));
}

beforeEach(() => {
  timeouts.expired.length = 0;
  locationHref = '';
  axiosInstance.defaults.adapter = refuse as never;
  Object.defineProperty(window, 'location', {
    configurable: true,
    value: {
      pathname: '/files',
      set href(value: string) {
        locationHref = value;
      },
      get href() {
        return locationHref;
      },
    },
  });
});

afterEach(() => {
  axiosInstance.defaults.adapter = originalAdapter;
});

describe('external-auth 401 handling', () => {
  it('session_expired goes through the timeout path, not a bare redirect', async () => {
    detail = { code: 'session_expired', message: 'Your session has expired.' };

    await expect(axiosInstance.get('/files')).rejects.toBeDefined();

    expect(timeouts.expired).toEqual(['absolute']);
    expect(locationHref).toBe('');
  });

  it('any other 401 still redirects to /login (calibrates the test above)', async () => {
    detail = 'Could not validate credentials';

    await expect(axiosInstance.get('/files')).rejects.toBeDefined();

    expect(timeouts.expired).toEqual([]);
    expect(locationHref).toBe('/login');
  });
});
