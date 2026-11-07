/**
 * Who may place a legal hold from the UI, and through which API (legal-hold plan, Lane D).
 * Regular users must never be offered a place or release control; an organization admin with
 * `org.legal_hold` acts for their organization; an instance admin places an operator hold.
 */
import { describe, it, expect } from 'vitest';
import type { CapabilitiesState } from '$stores/capabilities';
import { holdAuthorityForCaller, holdReasonValid } from './authority';

function caps(over: Partial<CapabilitiesState> = {}): CapabilitiesState {
  return {
    edition: 'cloud',
    loaded: true,
    capabilities: { 'org.legal_hold': true },
    audience: { 'org.legal_hold': 'org_admin' },
    maxUploadBytes: undefined,
    ...over,
  };
}

describe('holdAuthorityForCaller', () => {
  it.each([
    [
      'org admin, cap on, managed edition',
      { role: 'user', org_role: 'org:admin' },
      caps(),
      true,
      'organization',
    ],
    [
      'org admin who is also an instance admin',
      { role: 'admin', org_role: 'org:admin' },
      caps(),
      true,
      'organization',
    ],
    ['instance admin, community edition', { role: 'admin' }, caps(), false, 'platform'],
    ['super admin, community edition', { role: 'super_admin' }, caps(), false, 'platform'],
    [
      'instance admin in the managed edition, no org role',
      { role: 'admin', org_role: 'org:member' },
      caps(),
      true,
      'platform',
    ],
    ['regular user', { role: 'user' }, caps(), false, null],
    ['org member, cap on', { role: 'user', org_role: 'org:member' }, caps(), true, null],
    [
      'org admin, cap off',
      { role: 'user', org_role: 'org:admin' },
      caps({ capabilities: { 'org.legal_hold': false } }),
      true,
      null,
    ],
    [
      'org admin, cap aimed at members',
      { role: 'user', org_role: 'org:admin' },
      caps({ audience: { 'org.legal_hold': 'user' } }),
      true,
      null,
    ],
    [
      'org admin in a community build',
      { role: 'user', org_role: 'org:admin' },
      caps(),
      false,
      null,
    ],
  ] as const)('%s', (_label, caller, state, cloud, expected) => {
    expect(holdAuthorityForCaller(state, caller, cloud)).toBe(expected);
  });

  it('is null for nobody signed in', () => {
    expect(holdAuthorityForCaller(caps(), null, true)).toBeNull();
  });
});

describe('holdReasonValid', () => {
  it('accepts 10 to 2,000 characters after trimming, nothing else', () => {
    expect(holdReasonValid('  123456789  ')).toBe(false);
    expect(holdReasonValid('1234567890')).toBe(true);
    expect(holdReasonValid('x'.repeat(2000))).toBe(true);
    expect(holdReasonValid('x'.repeat(2001))).toBe(false);
  });
});
