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

describe('localPasswordAllowed', () => {
  it.each([
    ['local', false, true],
    ['oidc', true, true],
    ['pki', true, true],
    ['oidc', false, false],
    ['ldap', false, false],
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

  it('a super_admin on a super_admin row may do everything but link an identity', () => {
    const actions = permittedRowActions(superAdmin, target({ role: 'super_admin' }));
    expect(actions.linkIdentity).toBe(false);
    expect(actions.changeRole).toBe(true);
    expect(actions.resetPassword).toBe(true);
  });

  it('a plain admin is not offered a password reset on a super_admin row', () => {
    expect(permittedRowActions(admin, target({ role: 'super_admin' })).resetPassword).toBe(false);
  });

  it('does not offer lock on an account that is already inactive', () => {
    expect(permittedRowActions(superAdmin, target({ is_active: false })).lock).toBe(false);
    expect(permittedRowActions(superAdmin, target({ is_active: undefined })).lock).toBe(true);
  });

  it.each([
    ['oidc', true],
    ['pki', true],
    ['local', false],
    ['ldap', false],
  ])('local-fallback toggle for a super_admin: auth_type=%s -> %s', (authType, expected) => {
    const row = target({ auth_type: authType });
    expect(permittedRowActions(superAdmin, row).toggleLocalFallback).toBe(expected);
    expect(permittedRowActions(admin, row).toggleLocalFallback).toBe(false);
  });
});
