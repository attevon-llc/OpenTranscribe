/**
 * Pins what a user reads when an auth request fails, per status. These strings are the
 * only feedback on the sign-in, registration, PKI and MFA screens; a status mapped to
 * the wrong key (a 429 shown as "invalid credentials", a 401 MFA code shown as a server
 * error) sends the user to fix the wrong thing.
 */
import { describe, it, expect } from 'vitest';
import type { Translate } from '$lib/i18n';
import {
  classifyEnrollmentError,
  loginFailure,
  mfaVerifyFailure,
  passwordChangeFailure,
  pkiLoginFailure,
  registrationFailure,
} from './authErrors';

const echo: Translate = (key, opts) => (opts ? `${key}:${JSON.stringify(opts)}` : key);
const res = (status: number, data: Record<string, unknown> = {}, headers: unknown = {}) => ({
  response: { status, data, headers },
});

describe('loginFailure', () => {
  it('prefers the server detail on 401 and 400, with localised fallbacks', () => {
    expect(loginFailure(res(401, { detail: 'nope' }), echo).message).toBe('nope');
    expect(loginFailure(res(401), echo).message).toBe('auth.error.invalidCredentials');
    expect(loginFailure(res(400), echo).message).toBe('auth.error.invalidRequest');
  });

  it('reads Retry-After on 429', () => {
    expect(loginFailure(res(429, {}, { 'retry-after': '30' }), echo)).toEqual({
      message: 'auth.error.tooManyLoginAttemptsWait:{"seconds":30}',
      retryAfter: 30,
    });
    expect(loginFailure(res(429), echo)).toEqual({
      message: 'auth.error.tooManyLoginAttempts',
      retryAfter: null,
    });
  });

  it('never echoes server detail on a 5xx', () => {
    for (const status of [500, 502, 503]) {
      expect(loginFailure(res(status, { detail: 'stack trace' }), echo).message).toBe(
        'auth.error.serverError'
      );
    }
  });

  it('falls back through detail, message, generic for other statuses', () => {
    expect(loginFailure(res(403, { detail: 'verify email' }), echo).message).toBe('verify email');
    expect(loginFailure(res(418, { message: 'teapot' }), echo).message).toBe('teapot');
    expect(loginFailure(res(418), echo).message).toBe('auth.error.loginFailed');
  });

  it('distinguishes no response, a thrown error, and nothing at all', () => {
    expect(loginFailure({ request: {} }, echo).message).toBe('auth.error.networkUnreachable');
    expect(loginFailure({ message: 'boom' }, echo).message).toBe('auth.error.unexpected');
    expect(loginFailure({}, echo).message).toBe('auth.error.loginFailedCheckCredentials');
  });
});

describe('passwordChangeFailure', () => {
  it('shows a string detail, joins a validation list, or falls back', () => {
    expect(passwordChangeFailure(res(400, { detail: 'too short' }), echo)).toBe('too short');
    expect(passwordChangeFailure(res(422, { detail: [{ msg: 'a' }, { msg: 'b' }] }), echo)).toBe(
      'a. b'
    );
    expect(passwordChangeFailure(res(500), echo)).toBe('auth.forcedChange.failed');
    expect(passwordChangeFailure(undefined, echo)).toBe('auth.forcedChange.failed');
  });
});

describe('registrationFailure', () => {
  it('handles string, list and object details', () => {
    expect(registrationFailure(res(400, { detail: 'taken' }), echo)).toBe('taken');
    expect(registrationFailure(res(422, { detail: [{ msg: 'x' }, { message: 'y' }] }), echo)).toBe(
      'x. y'
    );
    expect(registrationFailure(res(400, { detail: { message: 'obj' } }), echo)).toBe('obj');
    expect(registrationFailure(res(400, { detail: { other: 1 } }), echo)).toBe('{"other":1}');
    expect(registrationFailure(res(500), echo)).toBe('auth.error.registrationFailed');
  });
});

describe('pkiLoginFailure', () => {
  it('maps a missing certificate, a disabled method, and anything else', () => {
    expect(pkiLoginFailure(res(401, { detail: 'x' }), echo)).toBe(
      'auth.error.pkiCertificateMissing'
    );
    expect(pkiLoginFailure(res(400, { detail: 'off' }), echo)).toBe('off');
    expect(pkiLoginFailure(res(400), echo)).toBe('auth.error.pkiNotEnabled');
    expect(pkiLoginFailure(res(500), echo)).toBe('auth.error.pkiFailed');
  });
});

describe('mfaVerifyFailure', () => {
  it('maps a bad code, a bad token, rate limiting, and anything else', () => {
    expect(mfaVerifyFailure(res(401), echo)).toBe('auth.error.mfaInvalidCode');
    expect(mfaVerifyFailure(res(401, { detail: 'wrong' }), echo)).toBe('wrong');
    expect(mfaVerifyFailure(res(400), echo)).toBe('auth.error.mfaInvalidToken');
    expect(mfaVerifyFailure(res(429, { detail: 'slow down' }), echo)).toBe(
      'auth.error.mfaTooManyAttempts'
    );
    expect(mfaVerifyFailure(res(500), echo)).toBe('auth.error.mfaFailed');
  });
});

describe('classifyEnrollmentError', () => {
  it('treats 401 as an expired half-token', () => {
    expect(classifyEnrollmentError(res(401), echo)).toEqual({
      kind: 'expired',
      message: 'auth.mfaEnroll.error.tokenExpired',
    });
  });

  it('classifies the 400 details narrowly, defaulting to retry', () => {
    const kind = (detail: string) => classifyEnrollmentError(res(400, { detail }), echo).kind;
    expect(kind('MFA is not available for your authentication type')).toBe('unavailable');
    expect(kind('MFA is not enabled on this system')).toBe('unavailable');
    expect(kind('MFA is already enabled')).toBe('restart');
    expect(kind('MFA setup not initiated')).toBe('restart');
    expect(kind('Invalid code')).toBe('retry');
    expect(classifyEnrollmentError(res(400, { detail: 'Invalid code' }), echo).message).toBe(
      'auth.mfaEnroll.error.invalidCode'
    );
  });

  it('retries any other failure, showing a string detail when there is one', () => {
    expect(classifyEnrollmentError(res(500, { detail: 'busy' }), echo)).toEqual({
      kind: 'retry',
      message: 'busy',
    });
    expect(classifyEnrollmentError(new Error('net'), echo)).toEqual({
      kind: 'retry',
      message: 'auth.mfaEnroll.error.setupFailed',
    });
  });
});
