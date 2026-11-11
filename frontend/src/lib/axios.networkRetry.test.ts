/**
 * A request that got no response at all (axios `ERR_NETWORK`) is replayed ONCE when its method
 * is safe. Chromium fails requests still queued for a connection with
 * `net::ERR_NETWORK_CHANGED` whenever the host's interfaces change, and XHR reports that as a
 * plain network error: the gallery's filter sidebar showed "could not load speakers" while the
 * API was answering 200, because the failed request had never reached it.
 *
 * Contract: one replay for GET/HEAD/OPTIONS; never a write; never a cancelled request, a
 * timeout or an HTTP error response; a failure that persists still rejects.
 */
import { describe, it, expect, beforeEach, afterEach, vi } from 'vitest';
import type { InternalAxiosRequestConfig, AxiosResponse } from 'axios';

vi.mock('$lib/edition', () => ({ isCloudEdition: false }));
vi.mock('$lib/auth/sessionRefresh', () => ({ refreshSession: vi.fn() }));

import axiosInstance, { abortAllRequests } from '$lib/axios';
import { NETWORK_RETRY_DELAY_MS, NETWORK_RETRY_JITTER_MS } from '$lib/utils/networkRetry';

type Outcome = 'network' | 'timeout' | number;
let calls: string[] = [];
let script: Outcome[] = [];

function adapter(config: InternalAxiosRequestConfig): Promise<AxiosResponse> {
  calls.push(`${(config.method ?? 'get').toUpperCase()} ${config.url}`);
  if (config.signal?.aborted) {
    return Promise.reject(
      Object.assign(new Error('canceled'), { code: 'ERR_CANCELED', name: 'CanceledError', config })
    );
  }
  const next = script.shift() ?? 200;
  if (next === 'network' || next === 'timeout') {
    // The shapes axios's XHR adapter rejects with: onerror and ontimeout, no `response`.
    return Promise.reject(
      Object.assign(new Error(next === 'network' ? 'Network Error' : 'timeout exceeded'), {
        code: next === 'network' ? 'ERR_NETWORK' : 'ECONNABORTED',
        config,
        request: {},
        isAxiosError: true,
      })
    );
  }
  const response = { status: next, statusText: '', data: {}, headers: {}, config } as AxiosResponse;
  if (next < 300) return Promise.resolve(response);
  return Promise.reject(
    Object.assign(new Error(`Request failed with status code ${next}`), {
      response,
      config,
      isAxiosError: true,
    })
  );
}

const original = axiosInstance.defaults.adapter;
const MAX_WAIT = NETWORK_RETRY_DELAY_MS + NETWORK_RETRY_JITTER_MS;

beforeEach(() => {
  calls = [];
  script = [];
  axiosInstance.defaults.adapter = adapter;
  vi.useFakeTimers();
});
afterEach(() => {
  vi.useRealTimers();
  vi.restoreAllMocks();
  axiosInstance.defaults.adapter = original;
});

describe('replay after a transport failure', () => {
  it('replays a GET once, after the delay, and resolves with the replay', async () => {
    vi.spyOn(Math, 'random').mockReturnValue(0);
    script = ['network', 200];
    const p = axiosInstance.get('/speakers');
    await vi.advanceTimersByTimeAsync(NETWORK_RETRY_DELAY_MS - 1);
    expect(calls).toEqual(['GET /speakers']);
    await vi.advanceTimersByTimeAsync(1);
    expect((await p).status).toBe(200);
    expect(calls).toEqual(['GET /speakers', 'GET /speakers']);
  });

  it('still rejects when the replay fails too: a real outage shows', async () => {
    script = ['network', 'network'];
    const p = axiosInstance.get('/speakers');
    const caught = p.catch((e: { code?: string }) => e);
    await vi.advanceTimersByTimeAsync(MAX_WAIT);
    expect(await caught).toMatchObject({ code: 'ERR_NETWORK' });
    expect(calls).toHaveLength(2);
  });

  it.each(['post', 'put', 'patch', 'delete'] as const)('never replays a %s', async (method) => {
    script = ['network'];
    const caught = axiosInstance.request({ url: '/files', method }).catch((e) => e);
    await vi.advanceTimersByTimeAsync(MAX_WAIT);
    expect(await caught).toMatchObject({ code: 'ERR_NETWORK' });
    expect(calls).toHaveLength(1);
  });

  it('does not replay a timeout or an HTTP error response', async () => {
    script = ['timeout', 500];
    const timedOut = axiosInstance.get('/a').catch((e) => e);
    const failed = axiosInstance.get('/b').catch((e) => e);
    await vi.advanceTimersByTimeAsync(MAX_WAIT);
    expect(await timedOut).toMatchObject({ code: 'ECONNABORTED' });
    expect(await failed).toMatchObject({ response: { status: 500 } });
    expect(calls).toEqual(['GET /a', 'GET /b']);
  });

  it('a sign-out during the wait cancels the replay instead of sending it', async () => {
    script = ['network'];
    const caught = axiosInstance.get('/speakers').catch((e) => e);
    await vi.advanceTimersByTimeAsync(1);
    abortAllRequests('User logged out');
    await vi.advanceTimersByTimeAsync(MAX_WAIT);
    expect(await caught).toMatchObject({ code: 'ERR_CANCELED' });
  });
});
