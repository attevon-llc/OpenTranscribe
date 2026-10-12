import { describe, it, expect } from 'vitest';
import { supportGrantErrorCode, isSessionEndingCode, type SupportGrantErrorCode } from './errors';

const err = (code: unknown, status = 403) => ({
  response: { status, data: { detail: { code, message: 'm' } } },
});

describe('supportGrantErrorCode', () => {
  it.each([
    'support_grant_expired',
    'support_grant_revoked',
    'support_grant_not_active',
    'support_grant_invalid',
    'support_access_unavailable',
    'support_grant_write_required',
    'support_grant_action_not_permitted',
    'support_access_audit_unavailable',
    'support_grant_already_decided',
    'support_grant_lapsed',
    'support_grant_self_approval',
    'support_grant_invalid_target',
    'support_grant_duration_exceeds_request',
  ])('extracts %s', (code) => {
    expect(supportGrantErrorCode(err(code))).toBe(code);
  });

  it('returns null for a plain 403 without a code', () => {
    expect(
      supportGrantErrorCode({ response: { status: 403, data: { detail: 'Forbidden' } } })
    ).toBe(null);
  });

  it('returns null for an unrelated code and for non-errors', () => {
    expect(supportGrantErrorCode(err('password_change_required'))).toBe(null);
    expect(supportGrantErrorCode(undefined)).toBe(null);
    expect(supportGrantErrorCode(new Error('x'))).toBe(null);
  });
});

describe('isSessionEndingCode', () => {
  it.each([
    'support_grant_expired',
    'support_grant_revoked',
    'support_grant_not_active',
    'support_grant_invalid',
    'support_access_unavailable',
  ] as SupportGrantErrorCode[])('%s ends the session', (code) => {
    expect(isSessionEndingCode(code)).toBe(true);
  });

  it.each([
    'support_grant_already_decided',
    'support_grant_write_required',
    'support_grant_action_not_permitted',
    'support_access_audit_unavailable',
    'support_grant_lapsed',
    'support_grant_self_approval',
  ] as SupportGrantErrorCode[])('%s does not end the session', (code) => {
    expect(isSessionEndingCode(code)).toBe(false);
  });

  it('null never ends the session', () => {
    expect(isSessionEndingCode(null)).toBe(false);
  });
});
