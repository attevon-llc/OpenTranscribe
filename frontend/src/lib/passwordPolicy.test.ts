import { describe, it, expect } from 'vitest';
import {
  buildPasswordRequirements,
  FALLBACK_PASSWORD_POLICY,
  type PasswordPolicy,
} from './passwordPolicy';

const nist: PasswordPolicy = {
  profile: 'standard',
  min_length: 15,
  require_uppercase: false,
  require_lowercase: false,
  require_digit: false,
  require_special: false,
  blocklist_enabled: true,
};

describe('buildPasswordRequirements', () => {
  it('lists the length, every composition rule, and nothing else for the stig-style policy', () => {
    expect(buildPasswordRequirements(FALLBACK_PASSWORD_POLICY).map((r) => r.key)).toEqual([
      'auth.passwordReqMinLength',
      'auth.passwordReqUppercase',
      'auth.passwordReqLowercase',
      'auth.passwordReqNumber',
      'auth.passwordReqSpecial',
    ]);
  });

  it('shows the server minimum, not a hardcoded one', () => {
    expect(buildPasswordRequirements(nist)[0]).toEqual({
      key: 'auth.passwordReqMinLength',
      params: { min: 15 },
    });
  });

  it('shows no composition rules and the blocklist note under nist', () => {
    expect(buildPasswordRequirements(nist).map((r) => r.key)).toEqual([
      'auth.passwordReqMinLength',
      'auth.passwordReqAnyCharacters',
      'auth.passwordReqNotBreached',
    ]);
  });

  it('omits the any-characters line when a custom profile still requires a digit', () => {
    const keys = buildPasswordRequirements({ ...nist, require_digit: true }).map((r) => r.key);
    expect(keys).toContain('auth.passwordReqNumber');
    expect(keys).not.toContain('auth.passwordReqAnyCharacters');
  });
});

describe('buildPasswordRequirements and the breached-password list', () => {
  it('promises the breached-password check only when a list is installed', () => {
    const installed = {
      installed: true,
      entries: 100000,
      source: 'default' as const,
      retrieved: null,
    };
    const keys = (status: typeof installed | undefined) =>
      buildPasswordRequirements({ ...nist, blocklist_status: status }).map((r) => r.key);
    expect(keys(installed)).toContain('auth.passwordReqNotBreached');
    expect(keys({ ...installed, installed: false, entries: 0 })).not.toContain(
      'auth.passwordReqNotBreached'
    );
    // An older server that does not report the status keeps the previous behaviour.
    expect(keys(undefined)).toContain('auth.passwordReqNotBreached');
  });
});
