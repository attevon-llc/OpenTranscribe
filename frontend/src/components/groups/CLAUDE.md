# frontend/src/components/groups

## Purpose

User-group management (create, rename, members, roles) rendered inside Settings → Groups. Groups
are the group-typed share targets used by `$components/sharing/*`.

## Key files

- `GroupsOverview.svelte` — the coordinator: loads `GroupsApi.fetchGroups()` into `groupsStore`,
  switches between the grid and the detail panel, and reloads on the `group-member-added` /
  `group-member-removed` **window events** dispatched by `$stores/websocket`.
- `GroupDetailPanel.svelte` — sub-coordinator for one group: inline name/description edit, delete,
  and `refreshGroup()` (a full `fetchGroupDetail`) after any member mutation. Derives
  `canEdit` / `canDelete` / `canAddMembers` from `group.my_role`.
- `GroupMemberList.svelte` — role change + remove per member. **Removing yourself is the "leave
  group" path**: it dispatches `left` (not `memberRemoved`) and swaps the confirm title/message.
- `GroupMemberSearch.svelte` — user search debounced 300 ms (min 2 chars), excludes
  `existingMemberUuids`, adds with the selected role, clears itself on success.
- `GroupRoleBadge.svelte` — owner/admin/member pill; the single place the role→color mapping lives.
- `GroupCreateModal.svelte` — name/description form over `ui/BaseModal`.

## Conventions / patterns

- Two-level ownership: `GroupsOverview` owns the group _list_ (in `groupsStore`);
  `GroupDetailPanel` owns the _selected group_ object and re-fetches it after children mutate.
  The member children call `GroupsApi` directly and dispatch — don't lift member state into the store.
- `my_role` drives every affordance; `$stores/groups` derives `myGroups` (owner) vs `memberGroups`.
- i18n via `$t`; toasts via `toastStore` + `getErrorMessage` on every failed call.

## How it connects

- Mounted by `$components/settings/GroupsSettings.svelte` (Settings → Groups).
- API: `$lib/api/groups`. Types: `$lib/types/groups`. Stores: `$stores/groups`, `$stores/auth`
  (self-detection in the member list), `$stores/toast`.

## Gotchas

- **Role gating here is cosmetic.** Enforcement is `_require_group_admin` in
  `backend/app/api/endpoints/groups.py` (owner/admin only, else 403). A hidden button is not a
  permission — never rely on `canEdit` for security.
- Group search/filtering is **client-side** (`filteredMyGroups` / `filteredMemberGroups`), which is
  fine because the full list is already loaded. Don't extend that pattern to media lists, which are
  server-filtered.
- No Playwright test selects into this folder (Groups is not in `test_settings_modal.py`'s
  `SECTIONS_TO_SWITCH`), so there is no regression net — verify changes manually in light + dark.
