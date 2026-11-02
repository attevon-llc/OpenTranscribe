import { describe, it, expect } from 'vitest';
import {
  canAssignElevatedRoles,
  isSuperAdmin,
  localPasswordAllowed,
  permittedRowActions,
  type ManagedUser,
  type RowActions,
  type Viewer,
} from './rowPermissions';

const admin: Viewer = { uuid: 'me', role: 'admin' };
const superAdmin: Viewer = { uuid: 'me', role: 'super_admin' };

function target(overrides: Partial<ManagedUser> = {}): ManagedUser {
  return { uuid: 't-1', role: 'user', auth_type: 'local', is_active: true, ...overrides };
}

function offered(actions: RowActions): string[] {
  return Object.entries(actions)
    .filter(([, allowed]) => allowed)
    .map(([name]) => name)
    .sort();
}

describe('isSuperAdmin / canAssignElevatedRoles', () => {
  it.each([
    [superAdmin, true],
    [admin, false],
    [{ uuid: 'me', role: 'user' }, false],
    [null, false],
    [undefined, false],
  ])('%o -> %s', (viewer, expected) => {
    expect(isSuperAdmin(viewer)).toBe(expected);
    expect(canAssignElevatedRoles(viewer)).toBe(expected);
  });
});

describe('localPasswordAllowed mirrors auth.utils.local_password_allowed', () => {
  it.each([
    ['local', false, true],
    ['local', true, true],
    ['pki', true, true],
    ['oidc', true, true],
    ['pki', false, false],
    ['oidc', false, false],
    ['ldap', true, false],
    ['saml', true, false],
    ['something-new', true, false],
    [undefined, true, false],
  ])('auth_type=%s fallback=%s -> %s', (authType, fallback, expected) => {
    expect(
      localPasswordAllowed(target({ auth_type: authType, allow_local_fallback: fallback }))
    ).toBe(expected);
  });
});

describe('permittedRowActions', () => {
  it('offers nothing on the viewer’s own row, or with no viewer', () => {
    expect(offered(permittedRowActions(superAdmin, target({ uuid: 'me' })))).toEqual([]);
    expect(offered(permittedRowActions(null, target()))).toEqual([]);
  });

  it('a super_admin gets every action on an ordinary local account except the fallback toggle', () => {
    expect(offered(permittedRowActions(superAdmin, target()))).toEqual([
      'changeRole',
      'delete',
      'editExpiration',
      'forceLogout',
      'linkIdentity',
      'lock',
      'resetMfa',
      'resetPassword',
    ]);
  });

  it('a plain admin gets only the admin-tier actions on an ordinary account', () => {
    expect(offered(permittedRowActions(admin, target()))).toEqual([
      'delete',
      'editExpiration',
      'forceLogout',
      'lock',
      'resetPassword',
    ]);
  });

  it('a plain admin on a super_admin row is offered none of the flagged actions', () => {
    expect(offered(permittedRowActions(admin, target({ role: 'super_admin' })))).toEqual([]);
  });

  it('a super_admin on a super_admin row may do everything but link an identity', () => {
    const actions = permittedRowActions(superAdmin, target({ role: 'super_admin' }));
    expect(actions.linkIdentity).toBe(false);
    expect(offered(actions)).toEqual([
      'changeRole',
      'delete',
      'editExpiration',
      'forceLogout',
      'lock',
      'resetMfa',
      'resetPassword',
    ]);
  });

  it('does not offer lock on an account that is already inactive', () => {
    expect(permittedRowActions(superAdmin, target({ is_active: false })).lock).toBe(false);
    expect(permittedRowActions(superAdmin, target({ is_active: undefined })).lock).toBe(true);
  });

  it.each([
    ['oidc', false, true],
    ['pki', false, true],
    ['local', false, false],
    ['ldap', false, false],
    ['saml', false, false],
    ['ldap', true, true],
    ['saml', true, true],
  ])(
    'local-fallback toggle for a super_admin: auth_type=%s fallback=%s -> %s',
    (authType, fallback, expected) => {
      const row = target({ auth_type: authType, allow_local_fallback: fallback });
      expect(permittedRowActions(superAdmin, row).toggleLocalFallback).toBe(expected);
      expect(permittedRowActions(admin, row).toggleLocalFallback).toBe(false);
    }
  );

  it('password reset needs both a password-capable account and authority over it', () => {
    const ldapRow = target({ auth_type: 'ldap', allow_local_fallback: true });
    expect(permittedRowActions(superAdmin, ldapRow).resetPassword).toBe(false);
    const oidcFallback = target({ auth_type: 'oidc', allow_local_fallback: true });
    expect(permittedRowActions(admin, oidcFallback).resetPassword).toBe(true);
    expect(permittedRowActions(admin, { ...oidcFallback, role: 'super_admin' }).resetPassword).toBe(
      false
    );
  });
});
