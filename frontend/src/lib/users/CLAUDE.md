# src/lib/users — user-management table logic

## Purpose

The logic behind `components/UserManagementTable.svelte` and `components/users/`, kept out of the
component so each rule is testable without rendering it.

## Key files

- `rowPermissions.ts` — **the single place the UI mirrors the server's user-admin gates**: which
  actions a row offers per viewer and target (role change, MFA reset, identity link and the
  local-fallback flag are super_admin-only; lock, session termination, account edits and delete
  refuse a super_admin target unless the caller is one; a local password exists only where
  `auth.utils.local_password_allowed` allows it). The server stays the authority. **When a backend
  gate changes, change the matching line here and its row in `rowPermissions.test.ts` in the same
  commit.**
- `accountActions.ts` — `createAccountActions(confirmDialog, onRefresh)`: the per-row mutations
  (role, delete, local-fallback flag, expiry, lock/unlock, force logout, MFA reset). Each reports
  through a toast, surfaces the backend `detail` verbatim, and refetches only when the rendered row
  is now wrong. Which actions a row _offers_ is `rowPermissions.ts`, not this module.
- `invitationsController.ts` — `createInvitationsController(confirmDialog)`: the invite form, the
  pending list and revocation. The invitation `status` is server-computed and rendered, never
  re-derived from `expires_at` / `used_at`.
- `accountExpiry.ts` (date-input <-> `account_expires_at`; a date means that day's last second) and
  `userSearch.ts` (`filterUsers`, case-insensitive name/email substring).

## Conventions / patterns

- The table **coordinates**; the controllers are **per-mount factories** (`create*`), not module
  singletons, so two mounts never share pending state.
