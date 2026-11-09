/**
 * A write refused by a read-only demo (403 `demo_read_only`) must tell the visitor once, in
 * the shared API error path, even when the clicked control was not disabled. The REAL toast
 * store is used so the dedupe and the hold-back of a call site's own error toast are measured,
 * not assumed.
 */
import { describe, it, expect, vi, beforeEach, afterEach } from 'vitest';
import { get } from 'svelte/store';
import type { AxiosResponse, InternalAxiosRequestConfig } from 'axios';

vi.mock('$lib/edition', () => ({ isCloudEdition: false }));
vi.mock('$stores/locale', async () => (await import('$testMocks/localeMocks')).keyLocale());

import axiosInstance from '$lib/axios';
import { toastStore } from '$stores/toast';

const originalAdapter = axiosInstance.defaults.adapter;
let reply: { status: number; data: unknown };

function adapter(config: InternalAxiosRequestConfig): Promise<AxiosResponse> {
  const response = {
    status: reply.status,
    statusText: '',
    data: reply.data,
    headers: {},
    config,
  } as AxiosResponse;
  if (reply.status < 300) return Promise.resolve(response);
  return Promise.reject(
    Object.assign(new Error('failed'), { response, config, isAxiosError: true })
  );
}

const REFUSAL = { detail: { code: 'demo_read_only', message: 'This is a read-only demo.' } };
const messages = () => get(toastStore).map((t) => `${t.type}:${t.message}`);

beforeEach(() => {
  vi.useFakeTimers();
  axiosInstance.defaults.adapter = adapter;
  toastStore.clear();
});
afterEach(() => {
  vi.useRealTimers();
  axiosInstance.defaults.adapter = originalAdapter;
});

describe('read-only demo refusal', () => {
  it('shows one translated toast for a refused write', async () => {
    reply = { status: 403, data: REFUSAL };

    await expect(axiosInstance.put('/files/x', {})).rejects.toBeTruthy();

    expect(messages()).toEqual(['error:demo.readOnlyToast']);
  });

  it('shows one toast for a burst of refused writes', async () => {
    reply = { status: 403, data: REFUSAL };

    await Promise.allSettled([
      axiosInstance.put('/a', {}),
      axiosInstance.post('/b', {}),
      axiosInstance.delete('/c'),
    ]);

    expect(messages()).toEqual(['error:demo.readOnlyToast']);
  });

  it("holds back the failing call site's own generic error toast", async () => {
    reply = { status: 403, data: REFUSAL };

    await axiosInstance.put('/files/x', {}).catch(() => toastStore.error('Failed to save'));

    expect(messages()).toEqual(['error:demo.readOnlyToast']);
  });

  it('does not hold back error toasts once the moment has passed', async () => {
    reply = { status: 403, data: REFUSAL };
    await axiosInstance.put('/files/x', {}).catch(() => {});
    vi.advanceTimersByTime(2500);

    toastStore.error('Something else broke');

    expect(messages()).toContain('error:Something else broke');
  });

  it.each([
    ['another 403', 403, { detail: { code: 'account_expired' } }],
    ['a 500', 500, { detail: 'boom' }],
  ])('says nothing for %s', async (_name, status, data) => {
    reply = { status, data };

    await axiosInstance.put('/files/x', {}).catch(() => {});

    expect(messages()).toEqual([]);
  });
});
