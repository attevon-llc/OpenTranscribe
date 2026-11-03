import { describe, it, expect, vi, beforeEach } from 'vitest';

const axios = vi.hoisted(() => ({ get: vi.fn(), post: vi.fn(), delete: vi.fn(), put: vi.fn() }));
vi.mock('$lib/axios', () => ({ default: axios }));

import {
  cancelSelfErasure,
  getSelfErasureStatus,
  putSelfErasureSettings,
  requestSelfErasure,
  selfErasureErrorKey,
} from './selfErasure';

beforeEach(() => vi.clearAllMocks());

describe('self-service erasure API', () => {
  it('uses the account holder routes, not the admin client', async () => {
    axios.get.mockResolvedValue({ data: { enabled: true } });
    axios.post.mockResolvedValue({ data: { ledger_uuid: 'u' } });
    axios.delete.mockResolvedValue({ data: { status: 'cancelled' } });

    expect(await getSelfErasureStatus()).toEqual({ enabled: true });
    await requestSelfErasure({ confirm: 'ERASE', password: 'p' });
    await cancelSelfErasure('abc');

    expect(axios.get).toHaveBeenCalledWith('/users/me/erasure-request');
    expect(axios.post).toHaveBeenCalledWith('/users/me/erasure-request', {
      confirm: 'ERASE',
      password: 'p',
    });
    expect(axios.delete).toHaveBeenCalledWith('/users/me/erasure-request/abc');
  });

  it('sends only the changed instance setting', async () => {
    axios.put.mockResolvedValue({ data: { enabled: true, grace_days: 14, max_grace_days: 21 } });
    await putSelfErasureSettings({ enabled: true });
    expect(axios.put).toHaveBeenCalledWith('/admin/self-erasure', { enabled: true });
  });

  it.each([
    ['last_super_admin', 'settings.accountErasure.errorLastSuperAdmin'],
    ['request_exists', 'settings.accountErasure.errorExists'],
    ['reauth_required', 'settings.accountErasure.errorReauth'],
    ['already_executing', 'settings.accountErasure.errorTooLate'],
    ['not_cancellable', 'settings.accountErasure.errorNotCancellable'],
    ['not_found', 'settings.accountErasure.errorNotFound'],
    ['something_new', 'settings.accountErasure.errorGeneric'],
  ])('maps %s to a fixed key', (code, key) => {
    expect(selfErasureErrorKey({ response: { data: { detail: { code } } } })).toBe(key);
  });

  it('falls back to the generic key for an error without a code', () => {
    expect(selfErasureErrorKey(new Error('boom'))).toBe('settings.accountErasure.errorGeneric');
  });
});
