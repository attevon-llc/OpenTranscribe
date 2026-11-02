/**
 * Which admin actions the user-management table offers, per viewer and target row.
 *
 * The server stays the authority; this is the table's one statement of what to offer,
 * so every rule can be read and tested in one place. When a backend gate changes,
 * change the matching line here and its row in `rowPermissions.test.ts`.
 *
 * - Role change, MFA reset, identity link and the local-fallback flag are
 *   super_admin-only (`get_current_active_superuser`; `PUT /users/{uuid}` strips
 *   `allow_local_fallback` from anyone else), as is minting an elevated account
 *   (the `ELEVATED_ROLES` gate on `POST /admin/users` and `POST /auth/invitations`).
 * - Unlock and file recovery are admin-tier with no target restriction, so the table
 *   offers them on every row but the viewer's own; they have no flag here.
 * - Identity linking refuses a super_admin target unconditionally (local-only by design).
 */

export const ROLE_SUPER_ADMIN = 'super_admin';

export interface Viewer {
  uuid: string;
  role: string;
}

export interface ManagedUser {
  uuid: string;
  role: string;
  auth_type?: string;
  allow_local_fallback?: boolean;
  is_active?: boolean;
}

export interface RowActions {
  changeRole: boolean;
  toggleLocalFallback: boolean;
  resetPassword: boolean;
  lock: boolean;
  forceLogout: boolean;
  resetMfa: boolean;
  linkIdentity: boolean;
  editExpiration: boolean;
  delete: boolean;
}

const NO_ACTIONS: RowActions = Object.freeze({
  changeRole: false,
  toggleLocalFallback: false,
  resetPassword: false,
  lock: false,
  forceLogout: false,
  resetMfa: false,
  linkIdentity: false,
  editExpiration: false,
  delete: false,
});

export function isSuperAdmin(viewer: Viewer | null | undefined): boolean {
  return viewer?.role === ROLE_SUPER_ADMIN;
}

/** Whether the viewer may create or invite `admin` / `super_admin` accounts. */
export function canAssignElevatedRoles(viewer: Viewer | null | undefined): boolean {
  return isSuperAdmin(viewer);
}

/** Whether the account can hold a local password at all. */
export function localPasswordAllowed(target: ManagedUser): boolean {
  return target.auth_type === 'local' || Boolean(target.allow_local_fallback);
}

/**
 * The actions offered on `target`'s row. The viewer's own row offers none: the table
 * renders it as "current user", and the server refuses self role change and delete.
 */
export function permittedRowActions(
  viewer: Viewer | null | undefined,
  target: ManagedUser
): RowActions {
  if (!viewer || viewer.uuid === target.uuid) return NO_ACTIONS;

  const superAdmin = isSuperAdmin(viewer);
  const targetOutranksViewer = target.role === ROLE_SUPER_ADMIN && !superAdmin;

  return {
    changeRole: superAdmin,
    toggleLocalFallback:
      superAdmin &&
      Boolean(target.auth_type) &&
      target.auth_type !== 'local' &&
      target.auth_type !== 'ldap',
    resetPassword: localPasswordAllowed(target) && !targetOutranksViewer,
    lock: target.is_active !== false,
    forceLogout: true,
    resetMfa: superAdmin,
    linkIdentity: superAdmin && target.role !== ROLE_SUPER_ADMIN,
    editExpiration: true,
    delete: true,
  };
}
