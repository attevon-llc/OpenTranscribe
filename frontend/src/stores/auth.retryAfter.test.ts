/** #788: a 429 from login shows the server's wait time, never a guessed one. */
import { describe, it, expect, vi, beforeEach } from 'vitest';

vi.mock('$lib/axios', () => ({
  default: { get: vi.fn(), post: vi.fn() },
  abortAllRequests: vi.fn(),
}));
vi.mock('$stores/locale', () => ({
  t: {
    subscribe: (run: (value: (key: string, opts?: Record<string, unknown>) => string) => void) => {
      run((key, opts) => (opts ? `${key}|${JSON.stringify(opts)}` : key));
      return () => {};
    },
  },
}));
vi.mock('$lib/session/clearUserState', () => ({ clearUserState: vi.fn() }));
vi.mock('$lib/edition', () => ({ isCloudEdition: false }));

import axiosInstance from '$lib/axios';
import { login } from './auth';

const mockedPost = vi.mocked(axiosInstance.post);

describe('login 429 Retry-After', () => {
  beforeEach(() => vi.clearAllMocks());

  it('shows the wait time from a plain-object header', async () => {
    mockedPost.mockRejectedValueOnce({
      response: { status: 429, data: {}, headers: { 'retry-after': '42' } },
    });
    const result = await login('a@example.com', 'x');
    expect(result.retry_after).toBe(42);
    expect(result.message).toBe('auth.error.tooManyLoginAttemptsWait|{"seconds":42}');
  });

  it('reads AxiosHeaders-style .get()', async () => {
    mockedPost.mockRejectedValueOnce({
      response: {
        status: 429,
        data: {},
        headers: { get: (n: string) => (n === 'retry-after' ? '7' : undefined) },
      },
    });
    expect((await login('a@example.com', 'x')).retry_after).toBe(7);
  });

  it.each([undefined, 'soon', '', '-3', '0'])(
    'degrades to the generic message for Retry-After %j',
    async (value) => {
      mockedPost.mockRejectedValueOnce({
        response: {
          status: 429,
          data: {},
          headers: value === undefined ? {} : { 'retry-after': value },
        },
      });
      const result = await login('a@example.com', 'x');
      expect(result.retry_after).toBeNull();
      expect(result.message).toBe('auth.error.tooManyLoginAttempts');
      expect(result.message).not.toMatch(/NaN/);
    }
  );
});
