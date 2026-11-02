/**
 * Pins the full account-lifecycle code contract with
 * `backend/app/api/endpoints/auth/lifecycle_gates.py`. `$stores/auth`'s tests cover the
 * password and expiry codes through the store; until this file nothing pinned the
 * approval pair (v381), so dropping `account_rejected` from the list passed the whole
 * suite while a rejected user got a toast per refused request instead of the blocking
 * screen.
 */
import { describe, it, expect } from 'vitest';
import { readAccountLifecycle } from './accountLifecycle';

const forbidden = (detail: unknown) => ({ response: { status: 403, data: { detail } } });

describe('readAccountLifecycle', () => {
  it.each([
    'password_change_required',
    'account_expired',
    'banner_acknowledgment_required',
    'account_pending_approval',
    'account_rejected',
  ])('recognises %s', (code) => {
    expect(readAccountLifecycle(forbidden({ code, message: 'm' }))).toEqual({ code, message: 'm' });
  });

  it.each(['never_acknowledged', 'banner_text_changed'])('carries banner reason %s', (reason) => {
    expect(
      readAccountLifecycle(forbidden({ code: 'banner_acknowledgment_required', reason }))
    ).toEqual({ code: 'banner_acknowledgment_required', message: '', reason });
  });

  it('drops an unrecognised reason rather than passing it through', () => {
    expect(
      readAccountLifecycle(
        forbidden({ code: 'banner_acknowledgment_required', message: 'x', reason: 'other' })
      )
    ).toEqual({ code: 'banner_acknowledgment_required', message: 'x' });
  });

  it('ignores a non-string code and a non-string message', () => {
    expect(readAccountLifecycle(forbidden({ code: 7 }))).toBeNull();
    expect(readAccountLifecycle(forbidden({ code: 'account_rejected', message: 5 }))).toEqual({
      code: 'account_rejected',
      message: '',
    });
  });

  it('ignores a null detail', () => {
    expect(readAccountLifecycle(forbidden(null))).toBeNull();
  });
});
