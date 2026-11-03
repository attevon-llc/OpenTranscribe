# frontend/src/components/users

## Purpose

Children of `components/UserManagementTable.svelte` (the coordinator). Currently
`PasswordResetModal.svelte`: an admin setting a new local password on another account
(`PUT /users/{uuid}`), mounted only while open so every open starts with empty fields.

## Conventions / patterns

- What a row may offer is decided in `$lib/users/rowPermissions.ts`, the one place the UI mirrors
  the server's user-admin gates (see `src/lib/users/CLAUDE.md`). Don't re-implement a gate in a
  component; change it there, with the backend, in one commit.
