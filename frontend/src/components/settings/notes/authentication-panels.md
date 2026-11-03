# components/settings — Authentication panels

Moved intact from `frontend/src/components/settings/CLAUDE.md`. Read before touching `AuthenticationSettings.svelte` or any IdP form (LDAP, OIDC, SAML, PKI, proxy). Backend model: `backend/app/auth/CLAUDE.md`.

## Authentication panels

`AuthenticationSettings.svelte` loads each tab panel on demand (`authTabLoaders` in
`$lib/settings/sectionLoaders`, through `LazySection`), so opening the section no longer pulls every IdP
form. The configuration fetch runs in parallel with the active panel's chunk; panels that take config
wait for it behind a skeleton, self-contained ones (mappings, SCIM, directory sync, change history) do not.
`configsReady` starts false (the Local form must never paint coded defaults before the stored values
arrive, and mounting it early fetched the password policy twice) and a save re-reads configuration
without blanking the form. It is a tab shell over `LocalAuthSettings`, `LDAPSettings`,
**`OIDCSettings`** (renamed from `KeycloakSettings`, which was deleted — do not resurrect a
vendor-named panel), `PKISettings`, `ProxySettings` (trusted-header/reverse-proxy auth),
`SAMLSettings`, `GroupMappingSettings` (the "mappings" tab, LDAP/OIDC sources only — see
below), `SessionSettings`, `AuthMailDesignation` (the "mail" tab) and `AuthConfigAuditPanel`.
Twelve tabs: local, ldap, oidc, pki, proxy, saml, mappings, scim, directorySync, session, mail, audit.

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
  the panel) and its i18n keys live under `settings.authentication.authMail.*`. The "no
  transport" state is rendered by the shared `ui/AuthMailNotice` (issue #1240), not by a paragraph
  of its own: pass `readiness={designation}`. The panel emits `change` on every read/save, and
  `AuthenticationSettings` listens so the **attention dot** on the tab clears the moment a
  designation fixes it. That dot is fetched by the strip itself on mount
  (`AuthConfigApi.getAuthMailReadiness()`), NOT by the panel: the panel is a lazy chunk, so a dot
  that waited for it would only appear after the admin had already clicked in. Do not register
  the read in `sectionData` PREFETCHERS; a cached value would keep the dot up after a save.
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
