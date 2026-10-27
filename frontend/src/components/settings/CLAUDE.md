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
- **After the modal opens, the rest of Settings is warmed in the background** (#1244). `SettingsModal`
  hands the sidebar rows the user can OPEN (locked rows are never passed) to
  `startIdlePrefetch` in `$lib/settings/sectionLoaders`: one idle slice at a time through
  `$lib/settings/idleQueue` (`requestIdleCallback`, `setTimeout` on Safari), 2 imports in flight, started
  400 ms after open so the landing section paints first, skipped on `navigator.connection.saveData` /
  2g, cancelled when the modal closes. Hover/focus on a row (`prefetchSectionIntent`) warms that row at once.
  Data follows code, and only for the registered cheap reads in `$lib/settings/sectionData`
  (`PREFETCHERS`: pipeline view, file-status summary; admin: retention; super_admin: auth config).
  **Never register system statistics, index health or anything a worker computes there.**
- **Section data is stale-while-revalidate, not a TTL cache.** `sectionData` remembers the last answer of
  those reads (10 min max age): a panel paints it at once and refreshes behind it, with a skeleton only on
  a cold miss; a refresh equal to what is shown is skipped, a failed refresh keeps what is shown, and a
  refresh never overwrites edits (retention checks `hasChanges`). It is cleared by
  `purgeTenantDataCaches()` (logout, login, support-session edges), a save drops/replaces its key
  (`invalidateSectionData` / `setSectionData`), and the pipeline key is dropped when any section's dirty
  flag clears. A new cached read = a key in `SectionDataKey`, a line in `PREFETCHERS` only if it is cheap,
  and a test that the role gate holds.
- **Linking out of Settings to a full page** (File status's "Open full screen"): the modal closes on every
  route change, so the link stamps its history entry (`stampSettingsReturn`) and carries
  `?from=settings&section=<id>` (`$lib/settings/returnToSettings`). Browser Back is handled by
  `SettingsModal`'s `afterNavigate` (popstate + stamp reopens the modal; not the root layout, whose static
  size is budgeted); the page shows a "Back to Settings"
  control from the query (works after a reload or in a new tab). Any other full page that a panel links
  to should reuse both halves rather than invent a third.
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
- `FileStatusSettings.svelte` — the condensed File Status (System group, every user): status counts,
  files needing attention and the last 24 h of uploads from `GET /my-files/status` (same `apiCache`
  key as the full page), plus an "Open full screen" **anchor** to `/file-status` (an `<a>`, so it
  middle-clicks and the modal closes itself on the route change). The task grid, filters and detail
  modal stay on the full page (`UserFileStatus` + `fileStatus/`); never grow a second copy here.
- `FileStatusSection.svelte` — the tab shell of the `file-status` row: **Status** (`FileStatusSettings`, everyone)
  and **Recovery** (`AdminTaskHealthPanel`, admins with `admin.task_health`; the old Task Health Monitor row).
  Tabs come from `$lib/settings/fileStatusTabs` (a user with one tab gets no strip); `admin-task-health` is an alias
  (`statusTabFor`) that opens Recovery. The modal renders the row title; each panel shows only its own
  description, so never add a second heading in a panel. The shell forwards the Recovery panel's `requestConfirm`.
- `SystemStatisticsPanel.svelte`, `AdminTaskHealthPanel.svelte` — admin dashboards (export
  module-context interfaces consumed by the shell).
- `*Modal.svelte` (ASRConfigModal, LLMConfigModal) — nested editors opened from a panel.
  `LLMConfigModal` delegates two blocks: `LLMLocalDiscovery` (the local-server detection, an
  `ExpandableSection` disclosure: hidden for openai/anthropic/openrouter/bedrock, collapsed with its
  result in the summary for ollama/vllm/custom, expanded only with no provider chosen and a reachable
  endpoint; the parent keeps `useDiscoveredEndpoint` and its `await tick()` ordering) and
  `LLMBedrockFields` (region + write-only AWS key; credentials are never pre-filled, a blank field is
  not sent, `clear_aws_credentials` is the only way to drop a saved key, an admin also sees the
  server-credential status).

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
  Every tab strip needs a matching `role="tabpanel"` with `id="tabpanel-<tabId>"` and
  `aria-labelledby="tab-<tabId>"` (the shared `ui/Tabs` points `aria-controls`/`id` at those names), and
  tab ids are namespaced per section (`chat-general`, `tx-language`) so panels in one modal never collide.
  The Transcription provider tab exists when `asr.user_providers` OR `asr.model_selection` is on; `ASRSettings`
  takes `userProvidersCap`/`modelSelectionCap` and, with the former off, hides the provider list, status and
  policy and never calls those (404ing) endpoints, leaving the local model and its library (#1207).
  Deep links go through `SECTION_ALIASES` (`watch-sources-email`/`-global` -> `watchSourcesTabFor`; Chat's
  `chat-admin` opens `chat-advanced`).
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
- **The Speaker Engine tab is progressive disclosure** (#1243 item 5): one line of purpose per control, the
  decision up front, detail behind a disclosure. `EngineIntro` shows the sidecar chip and one sentence, with
  the explanation and the admin env-var note under "How it works". `EngineSettingRow` is the one row chrome:
  the source badge and Reset button render only for an overridden value (a "Default" chip on every row was
  noise). The re-check cosine margin and max word duration sit under "Advanced re-check tuning" and the
  local-GPU scope is stated once, on the re-check toggle, never per control. Do not re-add per-control
  scope notes or a second explanation of Require Diarization Sidecar.
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
- **Every surface that picks or shows a transcription model uses `CapabilityBadges` and the catalog (#1243).**
  The pills (props), the one-line consequence (`explain`, from `capabilityNotes` in
  `$lib/settings/asrCapabilities`) and the key to the pills (`legend`) are ONE component; never
  hand-draw a speakers/timing flag or copy a model's capabilities into a component. A native
  `<select>` cannot hold pills, so its options carry the flags as ONE translated string
  (`optionWithFlags`, `settings.asrProvider.optionWithFlags` with `{{name}}`/`{{flags}}`, never
  fragments glued together) and the chosen option repeats them as badges below. Surfaces: Provider &
  Model (this server's model card, the transcription in use, each saved provider, the add-provider
  provider/model lists), the upload wizard and reprocess dialog (`components/PerFileModelSelect`, options
  from `$lib/settings/perFileModel`: "my setting" is the saved provider when one is active, never
  hard-coded to this server's model), and the Speaker Detection card (saved-provider list). A catalog
  entry the server does not list claims nothing (no timing pill), a provider whose models disagree reads
  "varies by model". The local model card's flags come from the catalog's `local` entry, not from
  `model_info` (that field says nothing about timing).
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
- `HuggingFaceTokenCard` (platform Hugging Face access token, super_admin) sits under `EngineSettings`
  on the Speaker Engine tab, so the tab's lock (admins see it locked, plain users never) is the card's
  lock. Write-only like the pyannote key: never pre-fill, cleared after save; Remove only for a token
  saved in Settings (a server-environment one is shown as such, not removable). View rules are in
  `$lib/settings/hfToken`; copy is `settings.engineSettings.hfToken.*` (found by settings search as part
  of the engine tab). Calls `/admin/hf-token` (`$lib/api/hfToken`).
- **Cloud provider policy** (#1246) is `asr/CloudProviderPolicy.svelte` (plus `CloudProviderPolicyRow`), mounted by
  `ASRSettings` under the saved-provider list on Transcription > Provider: an allow toggle per cloud provider, the
  "users may add their own keys" switch and one Save (`PUT /admin/asr-providers`; all allowed is `null`). A super
  administrator edits it; an admin gets it `locked` and **no request is made**; a plain user never gets it, but sees saved
  configs whose provider is no longer enabled marked "not enabled by your administrator" (activate disabled), and a note
  when no cloud provider is enabled. Credentials are write-only: the panel shows only "key set" chips, and "Add shared
  credentials" opens `ASRConfigModal` with `presetProvider` and `presetShared` (create mode only) instead of a second key
  form. A 409 `own_asr_keys_disabled` or `asr_provider_not_allowed` renders through `pipelineFailureKey`
  (`POLICY_REFUSAL_KEYS` in `pipelineKeys.ts`), never the server sentence.
- **Local models** (#1246) is `localModels/LocalModelLibrary.svelte` (+ `LocalModelRow`, `ActivateModelDialog`), mounted by
  `ASRSettings` under the model card for admins; it replaced the old select / Save / Restart controls, so no second path
  to set or restart the worker exists. Every route is super_admin: `locked` (an admin) renders the explanation and makes
  NO request. Row rules and every error sentence live in `$lib/settings/localModelView` (errors are fixed i18n sentences
  chosen by `detail.code`, never server text; keys are literal maps, not built from variables). Progress arrives on the
  `local-model-download` window event (`stores/websocket.ts`); while a download runs and the socket has been quiet for 4 s
  the catalog is re-read every 3 s (this also covers a reload mid-download). Activation (`$lib/settings/localModelActivation`)
  is set -> restart -> poll `GET /asr-settings/local-model/active` until EVERY worker named in the restart reply reports the
  new model with `ready_at` after `restart_requested_at`; the dialog states the worker restarts and requires confirmation, the
  in-flight job count (`active_tasks`) is only known from the restart reply, so it is shown after confirming, not in the dialog.
  `ASRSettings` refreshes only its model card on `activated` (a page-level `loadData` would unmount the library mid-message).
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
- **Help text: one description line, plus an `InfoTip` for the rest. No tinted "How it works" cards.**
  `transcription/InfoTip.svelte` is the one tip component (self-styled; it no longer depends on `.tx-form`):
  a real `<button>` (pass `label`, default `common.moreInformation`), opens on hover, focus and click,
  closes on Escape (captured, so the surrounding dialog stays open), outside click and blur, keeps the
  panel hoverable, clamps/flips inside the nearest clipping ancestor (RTL included) and takes rich slot
  content. Beside a modal-level title, wrap `<h3 class="section-title">` and the tip in `.section-title-row`
  (Organization Context does). Do not re-add a bottom/top `.info-box` card to a panel; the remaining ones
  (OIDC, SAML, SCIM token, Group mappings, Audio extraction) are candidates to convert the same way.
- **`OrgLegalHoldsPanel` (org admin, issue #1276)** is a lazy section (`org-holds`, `orgHolds`),
  shown only on `orgAdminCapOn(capState, 'org.legal_hold')`. It lists, places (a collection, or file
  ids one per line) and releases holds; the server decides what a hold may name and what stays held
  after a release, so the panel only keeps "Place"/"Release" disabled until the input is one the
  server accepts (reason 10 to 2,000 characters, valid uuids). The reason is shown in this panel
  and nowhere else. The "Legal hold" pill on a file header and a collection card is
  `ui/HoldBadge.svelte` over the server's `hold` field (who holds it, never why), and a delete,
  remove or reprocess refusal is translated by `getErrorMessage` from `held_by`
  (`HOLD_REFUSAL_KEYS` in `lib/i18n/keyMaps.ts`).
- **Privacy controls (issue #1280)** are four small surfaces over ONE restrict-only policy (platform,
  deployment, organization and user ANDed; any layer off turns the feature off; the server names the
  highest one as `locked_by`). All four use `components/privacy/PrivacySwitchCard` (a CONTROLLED switch:
  it asks the parent to change and shows what the server returns, so a refused save never leaves it
  lying) and `PurgeConfirmDialog`: `OrgPrivacyPolicyPanel` (org admin; lazy section `org-privacy`,
  `orgPrivacy` in `sectionLoaders`; shown only via `orgPrivacyVisible` in `$lib/settings/privacyPolicy`:
  managed edition AND `org.defaults` granted with audience `org_admin` AND `org_role === 'org:admin'`;
  lives in core, so a derived `$lib/cloud` still gets it), `InstancePrivacyCard` (super_admin, mounted
  on the Privacy & Redaction **policy** tab above `RedactionPolicySettings`), `UserPrivacyCard` (under
  Voice Attributes, beside `SpeakerAttributeSettings`) and the gender lock inside
  `SpeakerAttributeSettings` (read from `GET /privacy/effective`, shown disabled with the reason; the
  gender switch exists in ONE place and `UserPrivacyCard.reload()` is called when it is saved). Rules:
  switches apply immediately (no Save button, no dirty state); a purge button is enabled only once the
  switch is OFF at that layer (the API 409s `policy_still_enabled` otherwise) and the dialog needs a
  ticked box AND the typed word `PURGE`; the instance "everything" scope is offered only when
  `tenancyMode === 'single'` (unknown fails closed, the server refuses it in multi-tenant mode). Gender
  inference stays ON by default and every place it is shown or configured says it is inferred
  automatically and can be turned off (`privacy.gender.*`). The org **redaction floor** (#1279) is a
  later release: `OrgPolicy.redaction_floor` is in the contract but this client neither reads nor sends it.
- **Audit screens: `AuditLogViewer` (super_admin, platform-wide) and `OrgAuditLogPanel`
  (org admin, issue #1277) are two thin shells over the SAME `audit/AuditLogFilters.svelte` and
  `audit/AuditLogTable.svelte`.** `orgView` is the only difference: a member picker
  (`AuditUserPicker`, `GET /users/search`, tenant-limited, sends a uuid) and a resource-id field on
  the filter bar; resolved names (`actor_display`, `resource_display`, which the server fills in at
  read time and only inside the org) and a translated event label on the table. Don't fork the
  table for a third screen. The org panel is a lazy section (`org-audit-logs`, `orgAuditLog` in
  `sectionLoaders`), shown only on `orgAdminCapOn(capState, 'org.audit_logs')`; its event list
  offers the `content.*` types only while `audit.content_access` is available, and a 404 from the
  export hides the export buttons. Event labels are `ORG_AUDIT_EVENT_KEYS` in `lib/i18n/keyMaps.ts`
  (a literal-key map, with the raw identifier as fallback): add a key there, not a template string.
