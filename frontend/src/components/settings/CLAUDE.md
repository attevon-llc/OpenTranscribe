# frontend/src/components/settings

## Purpose

The ~40 settings panels rendered inside `SettingsModal.svelte`. Covers user prefs
(transcription, download, redaction, language…), admin config (ASR, engine, auth, retention,
embedding migration…), and dashboards (UserProfileSettings, SystemStatisticsPanel,
AdminTaskHealthPanel).

## Key files

- **Opening is two-stage and sections are lazy.** `routes/+layout.svelte` paints `SettingsModalShell`
  (frame + skeletons) the instant the store opens, while the large `SettingsModal` chunk loads, then
  swaps it in (`skipEnter`, no replayed animation). Both render `SettingsModalFrame` (the dialog
  chrome, E2E class names `.settings-modal` / `.settings-sidebar` / `.settings-content`). Every panel
  is loaded on first use through `LazySection` + `$lib/settings/sectionLoaders` (headings render
  at once, panel gets a skeleton and a retry state). The shell, modal and default section are
  prefetched on idle after sign-in and on user-menu hover/focus (`$lib/settings/settingsModalLoader`).
  New section = add a loader to `sectionLoaders` (and `LEAD_LOADER`), never a static import in the modal;
  keep `settingsModalLoader.ts` free of component imports (root layout budget). Tests that open a
  section should `await` the panel (`waitForOpen` in `SettingsModal.test.ts` waits out the skeleton).
- **Loading looks the same everywhere: `ui/SettingsSkeleton`** (`variant` form / rows / chips /
  dashboard, drawn in the shape of the content on the shared shimmer). `LazySection` shows it while a
  chunk loads and panels show it while their data loads, so there is no hand-made `.skeleton-*` box,
  bare "Loading..." text or page-sized spinner (`settingsLoadingStates.test.ts` fails on them).
  `SettingsModalShell` is the one allowed exception (it paints before anything else is loaded).
- **Every tab strip is `<Tabs sticky>`** (`ui/Tabs.svelte`; `wrap` for many tabs). Hand-rolled
  `role="tablist"` / `.tabs` rows fail `settingsTabsSticky.test.ts`.
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
  User-facing settings prose must read plainly: no env var names, badge values or internal engine
  names in it. If an admin needs the env var, put it in a separate admin-detail string
  (`settings.engineSettings.intro.overrideAdmin`). Interpolate defaults, never hardcode them in a
  value string (`languageDefaultsValue` takes `{{source}}`/`{{translate}}`) (#1220).
- Keep panels under ~300 lines; split large ones into nested modals.

## How it connects

- Parent: `SettingsModal.svelte`, opened via `$stores/settingsModalStore`. Backend admin/user
  settings endpoints through `$lib/api/*`.

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
  parent (see `CacheSettings` → `DataManagementSection`) instead of inventing a store key with no
  nav entry.
- **Sidebar grouping is by WHO changes it and HOW OFTEN, not by subject** (issue #861), with
  rows inside a group ordered by expected frequency of use. Keep groups at ~6 rows or fewer; if
  a group outgrows that, split it rather than appending. A group that needs a privilege carries
  `...(isAdmin ? [...] : [])` around the whole group; a privileged row inside an **ungated**
  group carries the same spread on the row, so the group stays visible to everyone else.
  The sidebar is a **column, not a scroller**: `SettingsSearch` is a fixed header and only
  `.sidebar-scroll` scrolls, so a scrollbar can never overlap the input. The tab strips of the four
  tabbed sections are sticky (`<Tabs sticky>`; `.settings-content` publishes `--tabs-sticky-inset`).

- **Tabbed sections decide their tabs from a pure helper, not inline.** Privacy & Redaction is ONE
  sidebar row (`content-redaction`, in the **AI & Chat** group right after Chat: its detectors are ML
  models on the redaction worker and its policy governs what an AI model may see; not an Account
  setting) over `PrivacyRedactionSettings`; `$lib/settings/privacyRedactionTabs`
  returns the tabs per role (plain user: personal only, no tab strip; admin: policy tab present but
  locked; super_admin: both). `redaction-policy` is NOT in `SECTION_MIN_ROLE` — it survives only as a
  deep-link/search alias that opens the policy tab, and the tab-level lock is the same super_admin tier.
  Watch Sources works the same way via `$lib/settings/watchSourcesTabs` (Sources for everyone; Email
  Notifications and Global Settings for super_admin). Add a tab by extending the helper and its test.
- **Transcription and Speaker Identification are two tabbed rows** under the group _Transcription &
  Speakers_ (words vs who spoke; different models, stages and roles). `TranscriptionSection` has
  `tx-language` / `tx-provider` / `tx-vocabulary` / `tx-accuracy`; `SpeakerIdentificationSection` has
  `spk-detection` / `spk-attributes` / `spk-engine` / `spk-maintenance`. Which tabs exist comes from
  `$lib/settings/transcriptionTabs` and `speakerIdentificationTabs` (capabilities and role), never inline.
  Tab ids are global DOM ids (`tab-<id>`), hence the `tx-` / `spk-` namespaces.
- **Data & Storage is one System row of six tabs** (`data-management`; `DataManagementSection`): File
  Retention, Media Cache, Database Backups, Data Integrity, Search & Indexing, Speaker Embedding System.
  Tabs come from `$lib/settings/dataManagementTabs` (admin; backups are super_admin and render locked for an
  admin; each tab also needs its `admin.*` capability). `retention`, `backup`, `data-integrity`,
  `search-indexing` and `embedding-migration` are aliases that open their tab (`dataTabFor`), judged for
  privilege by the row that holds them (`SECTION_MIN_ROLE['data-management']`). The Retention and Backup panels
  still report dirty state under their legacy ids; the sidebar dot for the row folds those in (`aliasIdsOf`).
  Cache dispatches `dirty` and the shell reports it as `data-management`.
- **Old section ids are aliases, not rows** (`$lib/settings/sectionAliases`): `asr-provider` and
  `custom-vocabulary` open Transcription at their tab, `engine-settings` and `speaker-attributes` open
  Speaker Identification at theirs. They stay in the `SettingsSection` union so deep links, settings
  search (`aliasSearchEntries` in the modal adds them when the tab is openable) and fixtures keep
  working; the sidebar highlights the row via `sidebarRowFor`, and the mobile `<select>` binds the
  effective id so an alias still has a matching option. Use `transcriptionTabFor` /
  `speakerIdTabFor` for the tab an id opens. `engine-settings` is NOT in `SECTION_MIN_ROLE`: the lock is
  the tab's.
- **Lock rule for the speaker tabs.** _Speaker Engine_ and _Maintenance_ are super_admin: plain users do
  not get the tab at all (they could never use it), admins see it **locked** (disabled, `title` tooltip
  naming the tier), super_admins use it. Deployment capabilities (`engine.settings`,
  `speaker_attributes.migration`, `asr.user_providers`, `vocab.user`, `transcription.prefs`) remove a tab
  entirely. The same rule applies inside panels: ASR's local-model controls and the embedding migration
  (`locked={!isSuperAdmin}`, no API calls while locked) render disabled for admins.
- **Section shells own dirty state; children only dispatch `change` `{ hasChanges }`.** The shell calls
  `settingsModalStore.setDirty('transcription' | 'speaker-identification', any)` and badges a dirty tab
  with `●`. Panels mount on first visit and then stay mounted (`hidden`), so edits survive a tab switch.
  Any child form in a shell must dispatch `change`, never call `setDirty` itself.
- _Maintenance_ holds `SpeakerAttributeBulkPanel` plus a link card that dispatches `navigate` with
  `embedding-migration`, which now opens the Speaker Embedding System tab of Data & Storage (no second copy).
- **E2E-guarded:** `.speaker-detection-settings` (its `.btn-primary` is the Save button, so
  `PyannoteCredentialForm` uses its own button classes), the modal-level `.section-title` must remain the
  first `.section-title` in `.settings-content`, and `#speaker-behavior`, `#min-speakers`, `#max-speakers`.
- **Processing pipeline** (`processing-pipeline`, first row of the Transcription & Speakers group, shown when
  `transcription.prefs` is on) is `PipelineOverview.svelte`: read-only, four step cards fed by
  `GET /user-settings/pipeline` (`$lib/api/pipeline`). It is mounted only while open, so every visit reads the
  current plan. Thin-frontend rule: the server resolved where each step runs and what is wrong; the page only
  picks copy. **Problems render from the plan's stable `code`** through `$lib/settings/pipelineIssues` /
  `pipelineSteps` / `pipelineKeys` (explicit literal keys, proven by `keyMaps.test.ts`); the server's `message`
  is shown only for a code this build has no copy for, never raw exception text. `IssueRow` is the one row
  component for a problem (glyph + sr-only severity, not colour alone) and `CapabilityBadges` the one pill set
  (_Speakers included / Text only_, _Word / Segment / No timing_, _Speakers via adapter (extra billing)_); the
  page, the Speaker Detection card and the ASR provider list/editor all use them. Links dispatch `navigate`
  with a section id; a code's fix tab is the table `FIX_SECTIONS` in `pipelineIssues.ts`.
  Keep `pipelineKeys.ts` out of `keyMaps.ts`: `keyMaps` is in the root layout's static closure (budget in
  `scripts/check-bundle-budget.mjs`), and putting these strings there pushed it over by 0.3 KB.
- **`SpeakerDetectionProviderCard` previews a CHOICE; the server decides.** `$lib/settings/speakerSources`
  mirrors `services/pipeline_plan.py` only to say, before saving, what the current transcription model means for
  the picked source (text only, no timing, lite) and where it would run. `PUT /user-settings/transcription`
  re-resolves the plan and answers 409 `{detail: {code, message}}`; `SpeakerDetectionSettings` reads
  `detail.code` (`speakerSaveErrorKey`) and never maps a bare 409 to "save your key first". Lite: the `local`
  option is not rendered (a stored one shows disabled with its reason). A locked source (#1109) renders the card
  read-only with the administrator note, not hidden. `asr_config` lists saved configs whose model returns
  speakers (excluding the transcribing one) and exists only with `asr.user_providers`. If the preview rules in
  `speakerSources.ts` and the plan ever disagree, the plan wins: change both.
- **Changing saved ASR providers reports its effect on the pipeline** (`$lib/settings/asrOutcome`): delete,
  set-active and clear-active return `{diarizer_source_reverted, pipeline_issues}`, create returns
  `auto_activated`; `changeMessages` / `notAutoActivatedMessages` turn them into messages keyed by code
  (standing notes such as `speaker_hints_ignored` are left out). A 409 `detail.code` is rendered through
  `pipelineFailureKey`: known code -> its sentence, unknown code -> the caller's generic key, never the
  server's `message`. Lite (`deployment_mode`): no `local` in the editor's provider list, no local model card,
  and `EngineSettings` swaps the local engine controls for the `local_diarization_unavailable` reason.
- `PyannoteCredentialForm` (the user's pyannote.ai key) renders under the Speaker Detection source select
  when `asr.user_providers` is on. The key is write-only: never pre-fill the input, never log it.
  Its states come from `$lib/settings/pyannoteCredential`.
- **Auto-Labeling** (`auto-labeling`) lives in the AI & Chat group; the panel and id are unchanged.

## Transcription forms

`TranscriptionLanguageSettings`, `TranscriptionAccuracySettings` and `SpeakerDetectionSettings` are three
self-contained forms; each saves only its own fields and resets only its own group
(`resetTranscriptionSettings(group)`), and reports dirty state by dispatching `change` `{ hasChanges }`
(never `setDirty`); the two section shells mount them and own the dirty flag. Shared cards, tooltips, buttons and styles live
in `transcription/` (styles are global CSS scoped under `.tx-form`). Deployment locks: `transcription.advanced`
hides and omits the VAD/accuracy fields; `transcription.diarization_source` makes the source read-only and omits it from the save.

## Gotchas

- **E2E-guarded selectors** in the shell: `.settings-modal`, `.settings-sidebar`,
  `.sidebar-section` (the group wrapper —
  `backend/tests/e2e/test_settings_modal.py`'s `NAV_GROUPS`), `.nav-item`, and
  `.settings-content .section-title` (the **content panel's** `<h3>`). Renaming any of them
  breaks Playwright tests — keep them stable. Note the sidebar **group title** is
  `.section-heading`, which is _not_ guarded and is a different element from `.section-title`.
- The modal closes itself on route change (`$page.url.pathname`) — don't re-add navigation logic.
- Watch Sources is a **user** feature whose email-config and global-settings blocks are
  super*admin; gate those on `isSuperAdmin`, not `isAdmin`, or a plain admin gets two swallowed
  403s and a panel that looks empty rather than forbidden.
  ⚠️ **That gate does NOT extend to the per-source notification modal.** Managing an email
  \_config* is super*admin (it holds mailbox credentials); \_linking* one to your own source is
  owner-level on the backend, and `WatchSourceEmailLinksModal` must stay ungated or the
  capability is unreachable for the people it exists for. Its picker reads the source-scoped
  `GET /{uuid}/emails/available`, not the super_admin `GET /email-configs`.
- `WatchSourcesSettings.svelte` is a **coordinator over `watchSources/`** (see that folder's
  CLAUDE.md) — it was 767 lines. It stays here rather than moving into the subfolder, matching
  `UserFileStatus.svelte` + `fileStatus/`; only the children moved.
- `SupportAccessStaffPanel.svelte` (Administration) and `SupportAccessApprovalsPanel.svelte`
  (Account) are the two settings faces of support-access grants (issue #1122); both render only
  when `tenancyMode === 'multi'`, and `SettingsModal` yields Escape to nested `BaseModal`s so
  closing a break-glass dialog never closes Settings. Detail: `../supportAccess/CLAUDE.md`.
- **Sliders: use `$components/ui/RangeSlider.svelte`, never a raw `<input type="range">`.** One
  design (the Content Redaction toxicity threshold: label, live value badge, native slider, hint)
  is shared by every settings slider; its CSS lives in `src/styles/range-slider.css`. Keep the
  element `id` (e2e/unit selectors use it), put a translated `hint` and any `InfoTip` through the
  `label-extra` slot, and pass `defaultValue` to show a "System default: X" line next to the
  control. `ui/rangeSliderStructure.test.ts` fails the build on a raw range input.
