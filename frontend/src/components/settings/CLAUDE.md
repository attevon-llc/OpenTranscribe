# frontend/src/components/settings

## Purpose

The ~40 settings panels rendered inside `SettingsModal.svelte`. Covers user prefs
(transcription, download, redaction, language…), admin config (ASR, engine, auth, retention,
embedding migration…), and dashboards (UserProfileSettings, SystemStatisticsPanel,
AdminTaskHealthPanel).

## Key files

- `../SettingsModal.svelte` — the **shell/router**: vertical sidebar nav + `{#if activeSection}`
  routing to one panel at a time. Owns `activeSection` (from `settingsModalStore`) and shared
  modals (ConfirmationModal, ProcessingDetailsModal).
- `UserProfileSettings.svelte` — account/profile section.
- `SystemStatisticsPanel.svelte`, `AdminTaskHealthPanel.svelte` — admin dashboards (export
  module-context interfaces consumed by the shell).
- `*Modal.svelte` (ASRConfigModal, LLMConfigModal) — nested editors opened from a panel.

## Conventions / patterns

- Each panel is self-contained: owns its own load/save against `$lib/api/*`; the shell only
  routes and passes `activeSection`. Import panels via `$components/settings/...`.
- Button styling uses the global `.btn`/`.btn-primary`/`.btn-secondary` classes (form-elements.css);
  cancel/reset buttons must hover grey, never blue. i18n via `$t`; light/dark parity required.
- Keep panels under ~300 lines; split large ones into nested modals.

## How it connects

- Parent: `SettingsModal.svelte`, opened via `$stores/settingsModalStore`. Backend admin/user
  settings endpoints through `$lib/api/*`.

## Authentication panels

`AuthenticationSettings.svelte` is a tab shell over `LocalAuthSettings`, `LDAPSettings`,
**`OIDCSettings`** (renamed from `KeycloakSettings`, which was deleted — do not resurrect a
vendor-named panel), `PKISettings`, `ProxySettings` (trusted-header/reverse-proxy auth),
`SAMLSettings`, `GroupMappingSettings` (the "mappings" tab, LDAP/OIDC sources only — see
below), `SessionSettings`, `AuthMailDesignation` (the "mail" tab) and `AuthConfigAuditPanel`.
Ten tabs total: local, ldap, oidc, pki, proxy, saml, mappings, session, mail, audit.

- **The Local tab renders one form whose fields belong to FOUR backend categories**
  (`local`, `password_policy`, `mfa`, `lockout`). `PUT /admin/auth-config/{category}` validates
  against a per-category schema and 400s on unknown keys, so the whole form cannot be PUT to
  `local`. `LOCAL_TAB_CATEGORY_KEYS` is the split; keep it in sync when adding a field.
- **A sensitive value never arrives.** The API sends `config_value: null` plus `is_set`, and the
  flattening in `transformConfigArray` preserves `<key>_is_set` so a panel can render
  "configured — leave blank to keep it". Never bind a placeholder into a password field: the
  next save would encrypt the placeholder over the real secret.
- **`SessionSettings` must send `terminate_oldest` / `reject`** for `concurrent_session_policy`.
  The panel used to offer `oldest`/`newest`/`all`, none of which the backend compares against,
  so the AC-10 limit silently enforced nothing.
- `AuthConfigAuditPanel` reads `auth_config_audit` (Postgres) — a **different source** from the
  Audit Log section, which streams security events out of OpenSearch and carries no
  configuration changes.
- `AuthMailDesignation.svelte` is the **Auth Email** tab of `AuthenticationSettings`. It used
  to live in `WatchSourcesSettings` (where the `EmailNotificationConfig` rows are managed), but
  that panel disappears when the `watch_sources` capability is off while
  `PUT /api/admin/auth-config/email/designation` stays reachable — so on those editions the
  setting could not be configured at all. It is mounted in exactly **one** place; don't render
  a second copy. It reads the config list from `getEmailConfigs()` (super_admin, same tier as
  the panel) and its i18n keys live under `settings.authentication.authMail.*`.
- `ActiveSessionsPanel.svelte` is mounted in `UserProfileSettings` — a user's own sessions, not
  an admin surface.
- **`GroupMappingSettings`** is the Group mappings tab — the only consumer of
  `/api/admin/group-mappings`. It splits into `GroupMappingForm` (create/edit over `BaseModal`)
  and `GroupMappingTester` (`POST /test`). Two rules are load-bearing: the role select offers
  **user/admin only** (`super_admin` is refused by the schema, the service and a DB CHECK, so
  offering it would only advertise an escalation path that does not exist), and the tester
  splits pasted claims on **newlines and semicolons, never commas** — a DN is full of commas,
  and a comma-split reports "unmatched" about a claim that resolves fine. The `POST /test`
  endpoint writes nothing; the panel says so rather than leaving it to be inferred.
- **`require_email_verification` and `require_account_approval`** are Local-tab fields in the
  `local` category (`LOCAL_TAB_CATEGORY_KEYS`). Approval sits in its own **un-dimmed** section:
  the registration block goes `pointer-events: none` when local passwords are off, but approval
  governs every newly provisioned account including external-IdP JIT, so an OIDC-only
  deployment must still be able to set it.
- **`PendingApprovalsPanel`** (Settings → User Management, above the table) works the queue that
  `require_account_approval` produces. A **409 means "already decided"** — say so and reload;
  never swallow it, and never retry, since a retry can only 409 again. The pending count is
  owned by `SettingsModal` (not the panel) because the sidebar badge has to show while the user
  is looking at some other section.

## Privilege gating (one source of truth)

`SettingsModal.svelte` owns `SECTION_MIN_ROLE` — the `'admin' | 'super_admin'` tier each gated
section needs, mirroring the FastAPI dependency (`get_current_admin_user` vs
`get_current_active_superuser`) on the endpoints that panel calls. The sidebar, the mobile
picker and the content router all read it, so a nav entry cannot disagree with the panel.

- **Never omit a nav entry the user merely lacks the privilege for.** Omission is what made a
  field admin conclude the LDAP configuration page did not exist. A section above the user's
  tier renders `disabled`, greyed, with a padlock and a `settings.nav.requiresSuperAdmin`
  tooltip; the pane shows an explicit `settings.permission.*` state rather than going blank.
  Omission stays reserved for **capabilities the deployment doesn't have** (`cap:`), which are
  genuinely absent.
- Adding a section: add its tier to `SECTION_MIN_ROLE` — not an ad-hoc `&& isAdmin` on the
  render block. Locked sections are excluded from the settings search index (a search hit
  promises a destination you can open).
- A panel nested inside another section has no section id of its own; report dirty state to the
  parent (see `CacheSettings` → `RetentionSettings`) instead of inventing a store key with no
  nav entry.

## Gotchas

- **E2E-guarded selectors** in the shell: `.settings-modal`, `.settings-sidebar`, `.nav-item`,
  `.section-title`. Renaming them breaks Playwright tests — keep them stable.
- The modal closes itself on route change (`$page.url.pathname`) — don't re-add navigation logic.
- Watch Sources is a **user** feature whose email-config and global-settings blocks are
  super_admin; gate those on `isSuperAdmin`, not `isAdmin`, or a plain admin gets two swallowed
  403s and a panel that looks empty rather than forbidden.
