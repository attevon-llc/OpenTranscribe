/**
 * 503 + `Retry-After` handling in `$lib/axios` (scale foundations lane A2).
 *
 * Contract: an overloaded server answers 503 with `Retry-After`. Idempotent requests
 * (GET/HEAD/OPTIONS) are retried ONCE after the advertised wait plus jitter, capped;
 * non-idempotent requests are never replayed and surface a translated "busy" message;
 * a 503 never reaches the 401 refresh / sign-out path.
 */
import { describe, it, expect, beforeEach, afterEach, vi } from 'vitest';
import type { InternalAxiosRequestConfig, AxiosResponse } from 'axios';

vi.mock('$lib/edition', () => ({ isCloudEdition: false }));
const refreshSession = vi.fn();
vi.mock('$lib/auth/sessionRefresh', () => ({ refreshSession }));

import axiosInstance from '$lib/axios';
import { getErrorMessage } from '$lib/utils/apiError';

// The interceptor loads the retry policy on the first 503; load it now so the fake clock below
// does not have to wait for a module fetch.
await import('$lib/utils/busyRetry');

let calls: string[] = [];
let script: Array<{ status: number; headers?: Record<string, string> }> = [];

function adapter(config: InternalAxiosRequestConfig): Promise<AxiosResponse> {
  calls.push(`${(config.method ?? 'get').toUpperCase()} ${config.url}`);
  const next = script.shift() ?? { status: 200 };
  const response = {
    status: next.status,
    statusText: String(next.status),
    data: {},
    headers: next.headers ?? {},
    config,
  } as AxiosResponse;
  if (next.status < 300) return Promise.resolve(response);
  return Promise.reject(
    Object.assign(new Error(`Request failed with status code ${next.status}`), {
      response,
      config,
      isAxiosError: true,
    })
  );
}

const original = axiosInstance.defaults.adapter;

beforeEach(() => {
  calls = [];
  script = [];
  refreshSession.mockReset();
  axiosInstance.defaults.adapter = adapter;
  vi.useFakeTimers();
});
afterEach(() => {
  vi.useRealTimers();
  vi.restoreAllMocks();
  axiosInstance.defaults.adapter = original;
});

const busy = (retryAfter = '1') => ({ status: 503, headers: { 'retry-after': retryAfter } });

describe('503 + Retry-After', () => {
  it('retries an idempotent GET once, not before Retry-After elapsed', async () => {
    vi.spyOn(Math, 'random').mockReturnValue(0);
    script = [busy('2'), { status: 200 }];
    const p = axiosInstance.get('/files');
    await vi.advanceTimersByTimeAsync(1999);
    expect(calls).toEqual(['GET /files']);
    await vi.advanceTimersByTimeAsync(1);
    const res = await p;
    expect(res.status).toBe(200);
    expect(calls).toEqual(['GET /files', 'GET /files']);
  });

  it('adds jitter of at most one second', async () => {
    vi.spyOn(Math, 'random').mockReturnValue(0.99);
    script = [busy('1'), { status: 200 }];
    const p = axiosInstance.get('/files');
    await vi.advanceTimersByTimeAsync(1900);
    expect(calls).toHaveLength(1);
    await vi.advanceTimersByTimeAsync(100);
    await p;
    expect(calls).toHaveLength(2);
  });

  it('caps a huge Retry-After', async () => {
    vi.spyOn(Math, 'random').mockReturnValue(0);
    script = [busy('3600'), { status: 200 }];
    const p = axiosInstance.get('/files');
    await vi.advanceTimersByTimeAsync(10_000);
    await p;
    expect(calls).toHaveLength(2);
  });

  it('retries only once: a second 503 is rejected', async () => {
    vi.spyOn(Math, 'random').mockReturnValue(0);
    script = [busy(), busy(), { status: 200 }];
    const p = axiosInstance.get('/files');
    const settled = p.catch((e) => e);
    await vi.advanceTimersByTimeAsync(5000);
    const err = await settled;
    expect(err.response.status).toBe(503);
    expect(calls).toHaveLength(2);
  });

  it('never replays a POST and surfaces the busy message', async () => {
    script = [busy(), { status: 200 }];
    const err = await axiosInstance.post('/files', {}).catch((e) => e);
    await vi.advanceTimersByTimeAsync(20_000);
    expect(calls).toEqual(['POST /files']);
    expect(getErrorMessage(err)).toBe('common.serverBusy');
  });

  it('does not retry a 503 without Retry-After', async () => {
    script = [{ status: 503 }, { status: 200 }];
    await axiosInstance.get('/files').catch(() => undefined);
    await vi.advanceTimersByTimeAsync(20_000);
    expect(calls).toHaveLength(1);
  });

  it('never refreshes the session or signs out on 503', async () => {
    vi.spyOn(Math, 'random').mockReturnValue(0);
    script = [busy(), busy()];
    const settled = axiosInstance.get('/auth/me').catch((e) => e);
    await vi.advanceTimersByTimeAsync(5000);
    await settled;
    expect(refreshSession).not.toHaveBeenCalled();
  });
});
