import { describe, it, expect } from 'vitest';
import {
  failureFocusField,
  forcedChangeError,
  isValidLoginIdentifier,
  loginFieldError,
} from './loginIdentifier';

describe('isValidLoginIdentifier', () => {
  it.each(['a@b.co', ' user@example.com ', 'jdoe', 'DOMAIN\\jdoe', 'first.last_1-x'])(
    'accepts %s',
    (id) => expect(isValidLoginIdentifier(id)).toBe(true)
  );
  it.each(['', ' ', 'a', 'has space', 'semi;colon', '$$'])('rejects %j', (id) =>
    expect(isValidLoginIdentifier(id)).toBe(false)
  );
});

describe('forcedChangeError', () => {
  it('requires all three fields before comparing them', () => {
    expect(forcedChangeError('', 'n', 'n')).toBe('auth.allFieldsRequired');
    expect(forcedChangeError('c', 'n', '')).toBe('auth.allFieldsRequired');
  });
  it('requires the new passwords to match', () => {
    expect(forcedChangeError('c', 'n1', 'n2')).toBe('auth.passwordsNoMatch');
    expect(forcedChangeError('c', 'n', 'n')).toBeNull();
  });
});

describe('loginFieldError', () => {
  it('reports the first problem in field order, and which input to focus', () => {
    expect(loginFieldError('  ', '')).toEqual({
      messageKey: 'auth.identifierRequired',
      fieldId: 'email',
    });
    expect(loginFieldError('a', 'pw')).toEqual({
      messageKey: 'auth.validIdentifierRequired',
      fieldId: 'email',
    });
    expect(loginFieldError('ann@example.com', '   ')).toEqual({
      messageKey: 'auth.passwordRequired',
      fieldId: 'password',
    });
  });

  it('accepts an email or an LDAP-style username with a password', () => {
    expect(loginFieldError('ann@example.com', 'pw')).toBeNull();
    expect(loginFieldError(' DOMAIN\\ann ', 'pw')).toBeNull();
  });
});

describe('failureFocusField', () => {
  it.each([
    [401, 'password'],
    [403, 'password'],
    [400, 'email'],
    [422, 'email'],
    [429, null],
    [500, null],
    [undefined, null],
  ] as const)('status %s focuses %s', (status, expected) => {
    expect(failureFocusField(status)).toBe(expected);
  });
});
