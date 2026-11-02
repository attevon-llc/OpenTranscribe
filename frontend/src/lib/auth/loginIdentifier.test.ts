import { describe, it, expect } from 'vitest';
import { forcedChangeError, isValidLoginIdentifier } from './loginIdentifier';

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
