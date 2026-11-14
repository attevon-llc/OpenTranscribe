import { describe, it, expect, vi, beforeEach } from 'vitest';
import { get } from 'svelte/store';
import type { AuthMethods } from '$stores/auth';

const ack = vi.hoisted(() => vi.fn());
const logoutFn = vi.hoisted(() => vi.fn());
const toastInfo = vi.hoisted(() => vi.fn());
vi.mock('$stores/auth', () => ({ acknowledgeBanner: ack, logout: logoutFn }));
vi.mock('$stores/toast', () => ({ toastStore: { info: toastInfo } }));
vi.mock('$stores/locale', async () => {
  const { readable } = await import('svelte/store');
  return { t: readable((k: string) => k) };
});

import { createLoginBanner } from './loginBanner';

const methods = (o: Partial<AuthMethods>) => ({ login_banner_enabled: false, ...o }) as AuthMethods;

beforeEach(() => vi.clearAllMocks());

describe('createLoginBanner', () => {
  it('applyMethods arms the modal only when the deployment enables the banner', () => {
    const b = createLoginBanner();
    b.applyMethods(methods({}));
    expect(get(b.enabled)).toBe(false);
    b.applyMethods(
      methods({
        login_banner_enabled: true,
        login_banner_text: 'Notice',
        login_banner_classification: 'CUI',
      })
    );
    expect([get(b.enabled), get(b.classification), get(b.consentPending)]).toEqual([
      true,
      'CUI',
      true,
    ]);
  });

  it('anonymous acknowledge only dismisses; it never calls the API', async () => {
    const b = createLoginBanner();
    b.armForHold();
    expect(await b.acknowledge(false)).toBe('dismissed');
    expect(ack).not.toHaveBeenCalled();
    expect(get(b.consentPending)).toBe(false);
  });

  it('a held user must reach the API; failure keeps the modal up with the message', async () => {
    const b = createLoginBanner();
    b.armForHold();
    ack.mockResolvedValueOnce({ success: false, message: 'nope' });
    expect(await b.acknowledge(true)).toBe('failed');
    expect(get(b.ackError)).toBe('nope');
    expect(get(b.consentPending)).toBe(true);
    expect(get(b.ackPending)).toBe(false);
    ack.mockResolvedValueOnce({ success: false });
    await b.acknowledge(true);
    expect(get(b.ackError)).toBe('loginBanner.acknowledgeFailed');
    ack.mockResolvedValueOnce({ success: true });
    expect(await b.acknowledge(true)).toBe('signed-in');
    expect(get(b.consentPending)).toBe(false);
    expect(get(b.ackError)).toBe('');
  });

  it('decline resets the form, logs out, re-arms the notice and then explains', async () => {
    const b = createLoginBanner();
    b.armForHold();
    await b.acknowledge(false);
    const order: string[] = [];
    logoutFn.mockImplementation(async () => order.push('logout'));
    toastInfo.mockImplementation(() => order.push('toast'));
    await b.decline(() => order.push('reset'));
    expect(order).toEqual(['reset', 'logout', 'toast']);
    expect(get(b.consentPending)).toBe(true);
    expect(toastInfo).toHaveBeenCalledWith('loginBanner.mustAcknowledge');
  });

  it('recordConsent is a no-op without a banner and tolerates a failed write', async () => {
    const b = createLoginBanner();
    await b.recordConsent();
    expect(ack).not.toHaveBeenCalled();
    b.armForHold();
    ack.mockResolvedValue({ success: false, message: 'x' });
    const warn = vi.spyOn(console, 'warn').mockImplementation(() => {});
    await expect(b.recordConsent()).resolves.toBeUndefined();
    expect(ack).toHaveBeenCalledTimes(1);
    warn.mockRestore();
  });
});
