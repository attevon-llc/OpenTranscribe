# Issue #861 — Settings Sidebar Regroup — Implementation Plan

Status: PLAN ONLY, not started. Written 2026-09-20 for issue
[#861](https://github.com/attevon-llc/OpenTranscribe/issues/861) (`feat(ui): regroup the settings
sidebar`, milestone v0.6.0, labels `enhancement` `frontend` `user-experience`
`epic:frontend-ui`). A fresh agent with no conversation memory should be able to execute this
end-to-end from this file alone.

**Verified against base commit `aeb4dc414557544be186a2a5104a9fe5fab1f188`**
(`aeb4dc41 Merge pull request #955 from attevon-llc/chore/v0.5.1-followup-dependency-batch`),
2026-09-20. Every line number below was read from that tree. Re-derive with
`git rev-parse HEAD` before trusting them; `SettingsModal.svelte` is edited often.

Branch for this work: `feat/v0.6.0-frontend-ux` (worktree
`.claude/worktrees/v0.6.0-frontend-ux`), shared with other v0.6.0 frontend lanes. Per the root
`CLAUDE.md` concurrency rule, **only one writer in that checkout commits** — if another lane is
active, hand over exact paths plus a commit message instead of running `git commit` yourself.

---

## ⚠️ Premise corrections — read before doing anything

Roughly half the issue bodies in this repo have been found to carry a wrong premise. Six were
found here. **Correcting them is part of the deliverable**, not commentary.

### PC1 — "the sidebar should be grouped and titled properly" — it ALREADY IS

The issue (inherited from #757 item 2) reads as though grouping is absent. It is not.
`SettingsModal.svelte:250-327` declares **seven** titled groups today, each rendered with an
`<h3 class="section-heading">`. The 2026-09-09 coherence audit's note — *"#757's settings sidebar
already has 7 titled groups. The defect is composition, not absence"* — **still holds exactly**
at `aeb4dc41`: the array is still at `:250-327`, still seven groups, byte-for-byte the shape the
app-chrome gist described.

The deliverable is therefore **which item belongs in which group, in what order, and what the
group is called** — not "add grouping".

### PC2 — the settings search index needs ZERO edits

The app-chrome gist warns *"a regroup must not orphan an entry from its index record."* That is
true but misleading about where the coupling is.
`frontend/src/lib/search/settingsSearchIndex.ts`'s `SECTION_NAMESPACES` is keyed by
**`SettingsSection` id**, never by group. And `visibleSections`
(`SettingsModal.svelte:349-353`) maps `{ id: item.id, label: item.label }` — the **item's** label,
not the group title. Group titles never enter the search corpus at all.

**Consequence:** as long as no section `id` changes and no item is dropped from the array, the
search index is untouched by this work. It still belongs on the keep-in-sync checklist (below)
because *adding* or *removing* an item would break it — but a pure regroup will not.

### PC3 — `.section-title` is NOT the sidebar group heading

`frontend/src/components/settings/CLAUDE.md`'s "E2E-guarded selectors" list reads
`.settings-modal`, `.settings-sidebar`, `.nav-item`, `.section-title`. In the current tree:

- `.section-heading` (`SettingsModal.svelte:777`, CSS `:1362`) is the **sidebar group title**.
- `.section-title` (`:872`, `:943`, `:952`, … ~25 occurrences, CSS `:1500`) is the **content
  panel's `<h3>`** — a different element entirely.
- `.sidebar-section` (`:776`, CSS `:1351`) is the sidebar group **wrapper**, and it **is**
  E2E-guarded — `backend/tests/e2e/test_settings_modal.py:359` defines
  `NAV_GROUPS = ".settings-sidebar .sidebar-section"` and asserts on it in four tests.

So the documented list is **wrong in one entry and missing another**. Fixing
`settings/CLAUDE.md` is a required edit in this plan (Edit 5).

### PC4 — `settings.sections.userSettings` is a dead i18n key in all 12 locales

`"settings.sections.userSettings"` exists in every locale (`en.json:62` = `"User Settings"`,
`ar.json:62`, `nl.json:62`, `fr.json:1738`, `de.json:1738`, `es.json:1738`, …) and
`rg -n "sections\.userSettings" frontend/src` matches **only the locale files**. No component
reads it. It is a leftover from a previous grouping. Delete it in all 12 (Edit 4).

### PC5 — `system-statistics` is NOT admin-gated, and the app opens on it

The app-chrome gist's §2.3 table lists group 1 `settings.sections.system` with no gate, which is
correct, but its recommended regrouping then folds `system-statistics` into an admin-only
"System" group. **That would remove the section from every non-admin's sidebar.**

- `system-statistics` is absent from `SECTION_MIN_ROLE` (`:104-118`) → open to any signed-in
  user, filtered only by the `system.hardware_stats` capability.
- `settingsModalStore.ts` `initialState.activeSection` is `'system-statistics'`, `open()` defaults
  to it (`:100-106`) and `close()` resets to it (`:107-113`).
- `Navbar.svelte:107` opens the modal with `settingsModalStore.open('system-statistics')` — the
  **primary entry point** for every user.

The plan below therefore keeps the `System` group **ungated** and item-gates the admin
maintenance rows inside it with the existing `...(isAdmin ? [...] : [])` pattern. See
**"What must NOT change"**.

### PC6 — `SECTIONS_TO_SWITCH`'s `"Download"` entry has never been able to match

`backend/tests/e2e/test_settings_modal.py:46-51`:

```python
SECTIONS_TO_SWITCH = [
    ("Profile & Security", "Profile"),
    ("Transcription Settings", "Transcription"),
    ("Recording Settings", "Recording"),
    ("Download", "Download"),
]
```

The `download` section's nav label is `$t('settings.download.title')`, which is
**`"URL Import Quality"`** (`en.json:286`) — it does not contain the substring `"Download"`. The
loop (`:183-201`) `continue`s when `nav_item.count() == 0` and then asserts `switched >= 3`, so
with three matchable entries out of four the test passes **at exactly its floor**. One more label
drift and it fails; meanwhile one of its four cases silently tests nothing. This is precisely the
"a test that cannot fail" class `scripts/audit-tests.py` exists for.

Fix it as part of this work (Edit 6) — the regroup is the moment someone is reading this file.

### PC7 (bonus, in the blast radius) — two redaction labels ship untranslated in 7 of 12 locales

`settings.contentRedaction.title` and `settings.redactionPolicy.title` are the literal English
`"Content Redaction"` / `"Redaction Policy"` in **de, es, fr, ja, pt, ru, zh**. `check:i18n`
passes because it enforces key **parity**, not translation — exactly the trap the root
`CLAUDE.md` documents. These are the two labels this regroup deliberately places **adjacent to
each other** in a new group, so shipping them side by side in English would make the defect
newly conspicuous. Translate them (Edit 4).

---

## 1. What exists today (the "before")

### 1.1 The declaration

`frontend/src/components/SettingsModal.svelte` — **1,711 lines**. The sidebar is **data**, not
markup: a reactive array at `:250-327`, post-processed at `:328-343`:

- `.map()` at `:328` filters items by capability (`capOn`, `:239-240`) — a capability the
  deployment lacks is **genuinely absent**.
- `.map()` at `:337` stamps `locked: sectionLocked(item.id, isAdmin, isSuperAdmin)` and
  normalises `badge` to a number — a **privilege** the user lacks greys the row, never removes it.
- `.filter()` at `:343` drops groups that ended up empty.

`SECTION_MIN_ROLE` (`:104-118`) is the **single source of privilege truth**; `sectionLocked()`
(`:129-138`) is the only consumer.

### 1.2 Three consumers, one array

| Consumer | Location | Reads |
|---|---|---|
| Desktop sidebar | `:767-811` | `section.title` → `<h3 class="section-heading">`; `section.items` → `<button class="nav-item">` |
| Mobile `<select>` picker | `:816-846` | `section.title` → `<optgroup label>`; `section.items` → `<option>` |
| Settings search index | `:349-357` via `visibleSections` | **items only** — group titles are not read (see PC2) |

The content router (`:848` onward) is a flat `{#if activeSection === '…'}` chain keyed by id; it
has no knowledge of groups. **A regroup is one edit to one array.** The mobile picker is
**untested** — verify it by hand (see Verification).

### 1.3 Before table — every section, its group, gate, i18n key, file:line

All lines are `frontend/src/components/SettingsModal.svelte` unless noted. "Tier" is the
`SECTION_MIN_ROLE` entry (`:104-118`); blank = open to any signed-in user. "Group gate" is the
spread guarding the whole group.

| # | Group (`title` key) | Group gate | Line | Section id | Item i18n key (label) | Tier | `cap` |
|---|---|---|---|---|---|---|---|
| **1** | `settings.sections.system` — "System" | — | `:252` | `system-statistics` | `settings.statistics.title` → *System Statistics* | — | `system.hardware_stats` |
| **2** | `settings.sections.billingTeam` — "Billing & Team" | `orgAdminCapOn` ×3 (cloud + `audience=org_admin`) | `:260-269` | `billing` | `billing.navLabel` | — | (audience) |
| | | | `:265` | `usage` | `usage.navLabel` | — | (audience) |
| | | | `:266` | `team` | `team.navLabel` | — | (audience) |
| **3** | `settings.sections.administration` — "Administration" | `isAdmin` (`:270`) | `:275` | `audit-logs` | `settings.auditLog.navLabel` → *Audit Logs* | `super_admin` | `audit.logs` |
| | | | `:276` | `authentication` | `settings.authentication.title` → *Authentication* | `super_admin` | `auth.config_ui` |
| | | | `:277` | `admin-users` | `settings.users.title` → *User Management* | `admin` | `users.local_admin` (+`badge`) |
| **4** | `settings.sections.systemManagement` — "System Management" | `isAdmin` (`:270`) | `:283` | `data-integrity` | `settings.dataIntegrity.title` → *Data Integrity* | `admin` | `admin.data_integrity` |
| | | | `:284` | `retention` | `settings.retention.title` → *File Retention* | `admin` | `admin.retention` |
| | | | `:285` | `backup` | `settings.backup.title` → *Database Backups* | `super_admin` | `admin.backup` |
| | | | `:286` | `search-indexing` | `settings.searchIndexing.title` → *Search & Indexing* | `admin` | `admin.search_indexing` |
| | | | `:287` | `embedding-migration` | `settings.embeddingMigration.title` → *Speaker Embedding System* | `admin` | `admin.embedding_migration` |
| | | | `:288` | `admin-task-health` | `settings.taskHealth.title` → *Task Health Monitor* | `admin` | `admin.task_health` |
| **5** | `settings.sections.account` — "Account" | — | `:295` | `groups` | `groups.title` → *Groups* | — | `sharing.teams` |
| | | | `:296` | `profile` | `settings.profile.title` → *Profile & Security* | — | — |
| **6** | `settings.sections.transcriptionAi` — "Transcription & AI" | — | `:302` | `ai-prompts` | `settings.aiPrompts.title` → *AI Summarization Prompts* | — | `prompts.user` |
| | | | `:303` | `asr-provider` | `settings.asrProvider.title` → *ASR Provider* | — | `asr.user_providers` |
| | | | `:304` | `engine-settings` | `settings.engineSettings.title` → *Engine Configuration* | `super_admin` | `engine.settings` — **item-gated `isAdmin`** |
| | | | `:305` | `redaction-policy` | `settings.redactionPolicy.title` → *Redaction Policy* | `super_admin` | `redaction.policy` — **item-gated `isAdmin`** |
| | | | `:306` | `auto-labeling` | `autoLabel.title` → *Auto-Label Settings* | — | — |
| | | | `:307` | `custom-vocabulary` | `settings.customVocabulary.title` → *Custom Vocabulary* | — | `vocab.user` |
| | | | `:308` | `content-redaction` | `settings.contentRedaction.title` → *Content Redaction* | — | `redaction.user` |
| | | | `:310` | `chat` | `chat.settings.title` → *Chat* | — | `chat.rag` |
| | | | `:311` | `llm-provider` | `settings.llmProvider.title` → *LLM Provider Configuration* | — | `llm.user_settings` |
| | | | `:312` | `organization-context` | `settings.orgContext.title` / `…cloudTitle` → *Organization Context* / *Transcript Context* | — | — |
| | | | `:313` | `speaker-attributes` | `settings.speakerAttributes.navTitle` → *Speaker Attributes* | — | — |
| | | | `:314` | `transcription` | `settings.transcription.title` → *Transcription Settings* | — | `transcription.prefs` |
| **7** | `settings.sections.mediaOutput` — "Media & Output" | — | `:320` | `audio-extraction` | `settings.audioExtraction.title` → *Audio Extraction* | — | — |
| | | | `:321` | `media-sources` | `settings.mediaSources.title` → *Media Sources* | — | — |
| | | | `:322` | `watch-sources` | `settings.watchSources.title` → *Watch Sources* | — | `watch_sources` |
| | | | `:323` | `recording` | `settings.recording.title` → *Recording Settings* | — | `recording` |
| | | | `:324` | `download` | `settings.download.title` → **_URL Import Quality_** | — | `exports` |

Not in the array: **`chat-admin`** — it is a `SettingsSection` (`settingsModalStore.ts:34`) with a
`dirtyState` slot and a `SECTION_NAMESPACES` entry, but no nav row; it is reached through the
Chat panel's Advanced tab. Leave it alone.

### 1.4 What is measurably wrong

1. **`transcriptionAi` is a dumping ground** — **12** rows for an admin, **10** for a user. No
   other group exceeds 6. It mixes user preferences (`transcription`, `custom-vocabulary`,
   `content-redaction`, `auto-labeling`) with **super-admin platform tuning**
   (`engine-settings`, `redaction-policy`, both item-gated inline at `:304-305`) and with things
   that are neither transcription nor AI (`speaker-attributes`).
2. **Order does not track importance.** Group 1 is `System` — a **single** row. `Account`
   (`profile`, the most universally used section) is group 5 of 7. Inside `transcriptionAi` the
   order is neither alphabetical, nor by frequency, nor admin-vs-user.
3. **Two near-identical labels sit four rows apart in the same group**: *Redaction Policy*
   (`:305`, super_admin) and *Content Redaction* (`:308`, user).
4. **Two adjacent admin groups** (`administration` `:272`, `systemManagement` `:281`) whose names
   do not tell you which one holds what — "Administration" vs "System Management" is a
   distinction without a rule.

---

## 2. The proposed grouping (the "after") — and the reasoning, which IS the deliverable

### 2.1 The organising principle

**Group by WHO changes it and HOW OFTEN — not by subject matter.** That is the axis the present
arrangement ignores, and it is the axis a settings sidebar is scanned along: a user arrives with
"I want to change *my* thing" or "I need to fix *the deployment*", never with "show me everything
tagged AI". Subject matter is what produced a 12-row `Transcription & AI` group that a user has
to read end-to-end because any row in it might be theirs.

Two hard rules fall out of it, and both are checkable:

- **No group exceeds ~6 rows.** A group you must read linearly is not a group.
- **Within a group, order by expected frequency of use**, most-used first — never
  alphabetically. Record that in a code comment so the next person does not re-alphabetize it.

### 2.2 After table

| Order | Group (`title` key) | Group gate | Rows, in order | Reasoning |
|---|---|---|---|---|
| **1** | `settings.sections.account` — **"Account"** *(key kept)* | — | `profile`, `groups` | Everyone, most often, no privilege needed. It is the one group that is certainly relevant to whoever opened the modal, so it leads. `profile` before `groups`: Profile & Security holds password/MFA/sessions and is opened far more often than team membership. Was group **5 of 7**. |
| **2** | `settings.sections.transcription` — **"Transcription"** *(NEW key)* | — | `transcription`, `asr-provider`, `custom-vocabulary`, `speaker-attributes`, `auto-labeling` | The core product loop, entirely user-owned. Splitting this out of the 12-row group is most of the fix. Order = frequency: you set transcription prefs constantly, pick an ASR provider rarely, and touch vocabulary/speaker attributes/auto-labels only when tuning output quality. `speaker-attributes` belongs here, not under "AI" — it describes how speakers are labelled on a transcript. |
| **3** | `settings.sections.aiChat` — **"AI & Chat"** *(NEW key)* | — | `chat`, `llm-provider`, `ai-prompts`, `organization-context` | LLM-backed features are a genuinely different concern from ASR, with a different failure mode (a model/provider you configure) and a different audience. `organization-context` moves here from `transcriptionAi` on evidence, not taste: `OrganizationContextSettings.svelte` persists `include_in_default_prompts` / `include_in_custom_prompts` — it is prompt context for the LLM, and nothing else reads it. |
| **4** | `settings.sections.privacyRedaction` — **"Privacy & Redaction"** *(NEW key)* | — (row-gated inside) | `content-redaction`, `redaction-policy` *(item-gated `isAdmin`)* | Puts the two confusable labels **adjacent**, where the user-setting / admin-policy distinction is visible in one glance and the padlock does the explaining — instead of four rows apart in a group about transcription. Privacy is also the thing users most often go looking for *by name*, and it had no name at all before. |
| **5** | `settings.sections.mediaOutput` — **"Media & Output"** *(key kept)* | — | `download`, `media-sources`, `watch-sources`, `recording`, `audio-extraction` | Already coherent; membership unchanged. Only re-ordered by frequency — URL import (`download`) and media sources are the everyday ingest path; `audio-extraction` is a one-time preference. |
| **6** | `settings.sections.billingTeam` — **"Billing & Team"** *(key kept)* | `orgAdminCapOn` ×3 | `billing`, `usage`, `team` | **Unchanged.** Cloud-only and `audience=org_admin`-only; the whole group drops out in community builds. Explicitly *not* merged with an "Organization" group — doing so would leave a one-row group in community, which is the defect being fixed. |
| **7** | `settings.sections.administration` — **"Administration"** *(key kept)* | `isAdmin` | `admin-users`, `authentication`, `engine-settings`, `audit-logs` | Reframed with an actual rule: **"who may do what, and how this deployment behaves"** — identity, access, and the engine that serves them. `admin-users` first (it carries the pending-approvals badge, so it is the one row with a live queue behind it). `engine-settings` moves here out of `transcriptionAi`: it is deployment tuning a super-admin does, not a transcription preference a user sets. |
| **8** | `settings.sections.system` — **"System"** *(key kept)* | — (admin rows item-gated) | `system-statistics`, then `isAdmin`-gated: `admin-task-health`, `search-indexing`, `data-integrity`, `embedding-migration`, `retention`, `backup` | **Maintenance and observability — rarely touched, correctly last.** This collapses the old one-row `System` group and the six-row `System Management` group into one bucket with a rule you can state: *looking at or repairing the deployment's state*, as opposed to *configuring its behaviour* (group 7). Order = frequency: you look at statistics and task health often, run an embedding migration or a backup restore almost never. The group stays **ungated** so `system-statistics` remains visible to every user exactly as today (**PC5**) — the six admin rows are item-gated with the same `...(isAdmin ? [...] : [])` spread already used at `:304-305`. |

### 2.3 What this buys, measured

| | Before | After |
|---|---|---|
| Groups a community **user** sees | 4 | 6 |
| Groups a community **admin** sees | 6 | 7 |
| **Largest group, user** | **10** (`Transcription & AI`) | **5** (`Transcription`, `Media & Output`) |
| **Largest group, admin** | **12** (`Transcription & AI`) | **7** (`System`) |
| One-row groups, admin | 1 (`System`) | 0 |
| Rows before reaching `profile` (user) | 3 | **0** |
| Untitled/ungrouped rows | 0 | 0 |

**Rows visible per persona are IDENTICAL before and after** — 18 for a community user, 29 for an
admin, 29 for a super-admin, with the same nine padlocked rows for a plain admin. Verify this;
see §6.

The trade is deliberate: *more* groups, each much smaller. Scannability comes from bounded group
size plus a title that predicts the contents, not from a short list of headings.

---

## 3. Exact edits

Five files, one of them ×12. No section `id` changes, no new sections, no deletions.

### Edit 1 — `frontend/src/components/SettingsModal.svelte:250-327` — replace the array

Replace the whole literal from the `$: sidebarSections = [` on `:250` up to and including the
closing `]` on `:327`. **Leave `:328-343` (the `.map`/`.map`/`.filter` chain) exactly as it is.**

```svelte
  // Define sidebar sections (filtered by capability; empty sections drop out).
  //
  // Grouped by WHO changes it and HOW OFTEN, not by subject matter (issue #861):
  // the user's own settings first, then shared/media concerns, then admin
  // configuration, then deployment maintenance last. Within every group, rows are
  // ordered by expected frequency of use — do NOT re-alphabetize them.
  //
  // Group gating vs row gating: a group carrying `...(isAdmin ? [...] : [])` is
  // invisible below that tier. A row inside an UNGATED group that needs privilege
  // is spread in the same way, which keeps the group (and its ungated siblings)
  // visible. `SECTION_MIN_ROLE` above stays the single source of privilege truth —
  // never add an ad-hoc guard on a render block.
  $: sidebarSections = [
    {
      title: $t('settings.sections.account'),
      items: [
        { id: 'profile' as SettingsSection, label: $t('settings.profile.title'), icon: 'user' },
        { id: 'groups' as SettingsSection, label: $t('groups.title'), icon: 'group', cap: 'sharing.teams' }
      ]
    },
    {
      title: $t('settings.sections.transcription'),
      items: [
        { id: 'transcription' as SettingsSection, label: $t('settings.transcription.title'), icon: 'waveform', cap: 'transcription.prefs' },
        { id: 'asr-provider' as SettingsSection, label: $t('settings.asrProvider.title'), icon: 'mic', cap: 'asr.user_providers' },
        { id: 'custom-vocabulary' as SettingsSection, label: $t('settings.customVocabulary.title'), icon: 'list', cap: 'vocab.user' },
        { id: 'speaker-attributes' as SettingsSection, label: $t('settings.speakerAttributes.navTitle'), icon: 'user' },
        { id: 'auto-labeling' as SettingsSection, label: $t('autoLabel.title'), icon: 'tag' }
      ]
    },
    {
      title: $t('settings.sections.aiChat'),
      items: [
        // Admin platform tuning for chat lives in this panel's Advanced tab, not a second row.
        { id: 'chat' as SettingsSection, label: $t('chat.settings.title'), icon: 'message', cap: 'chat.rag' },
        { id: 'llm-provider' as SettingsSection, label: $t('settings.llmProvider.title'), icon: 'brain', cap: 'llm.user_settings' },
        { id: 'ai-prompts' as SettingsSection, label: $t('settings.aiPrompts.title'), icon: 'message', cap: 'prompts.user' },
        // Org context is prompt material: OrganizationContextSettings persists
        // include_in_default_prompts / include_in_custom_prompts and nothing else reads it.
        { id: 'organization-context' as SettingsSection, label: isCloudEdition ? $t('settings.orgContext.cloudTitle') : $t('settings.orgContext.title'), icon: 'briefcase' }
      ]
    },
    {
      // The user setting and the admin policy sit together on purpose: their labels
      // are near-identical, and adjacency is what makes the padlock explain the
      // difference instead of the two reading as duplicates rows apart.
      title: $t('settings.sections.privacyRedaction'),
      items: [
        { id: 'content-redaction' as SettingsSection, label: $t('settings.contentRedaction.title'), icon: 'eye-off', cap: 'redaction.user' },
        ...(isAdmin ? [{ id: 'redaction-policy' as SettingsSection, label: $t('settings.redactionPolicy.title'), icon: 'shield', cap: 'redaction.policy' }] : [])
      ]
    },
    {
      title: $t('settings.sections.mediaOutput'),
      items: [
        { id: 'download' as SettingsSection, label: $t('settings.download.title'), icon: 'download', cap: 'exports' },
        { id: 'media-sources' as SettingsSection, label: $t('settings.mediaSources.title'), icon: 'link' },
        { id: 'watch-sources' as SettingsSection, label: $t('settings.watchSources.title'), icon: 'eye', cap: 'watch_sources' },
        { id: 'recording' as SettingsSection, label: $t('settings.recording.title'), icon: 'mic', cap: 'recording' },
        { id: 'audio-extraction' as SettingsSection, label: $t('settings.audioExtraction.title'), icon: 'file-audio' }
      ]
    },
    // Cloud edition — org-admin billing/usage/team. Gated by audience='org_admin'
    // so regular org members never see these. Community never reaches here
    // (isCloudEdition is false), so the section is empty and drops out.
    ...(orgAdminCapOn(capState, 'billing') || orgAdminCapOn(capState, 'usage_dashboard') || orgAdminCapOn(capState, 'organizations') ? [
      {
        title: $t('settings.sections.billingTeam'),
        items: [
          ...(orgAdminCapOn(capState, 'billing') ? [{ id: 'billing' as SettingsSection, label: $t('billing.navLabel'), icon: 'credit-card' }] : []),
          ...(orgAdminCapOn(capState, 'usage_dashboard') ? [{ id: 'usage' as SettingsSection, label: $t('usage.navLabel'), icon: 'activity' }] : []),
          ...(orgAdminCapOn(capState, 'organizations') ? [{ id: 'team' as SettingsSection, label: $t('team.navLabel'), icon: 'users' }] : [])
        ]
      }
    ] : []),
    // Administration = who may do what, and how this deployment behaves.
    // Listed for every admin; rows above the admin's tier render disabled — see sectionLocked().
    ...(isAdmin ? [
      {
        title: $t('settings.sections.administration'),
        items: [
          { id: 'admin-users' as SettingsSection, label: $t('settings.users.title'), icon: 'users', cap: 'users.local_admin', badge: pendingApprovalCount },
          { id: 'authentication' as SettingsSection, label: $t('settings.authentication.title'), icon: 'key', cap: 'auth.config_ui' },
          { id: 'engine-settings' as SettingsSection, label: $t('settings.engineSettings.title'), icon: 'cpu', cap: 'engine.settings' },
          { id: 'audit-logs' as SettingsSection, label: $t('settings.auditLog.navLabel'), icon: 'list', cap: 'audit.logs' }
        ]
      }
    ] : []),
    {
      // System = looking at or repairing the deployment's state (vs. Administration,
      // which configures its behaviour). Last because it is the least-often opened.
      // The GROUP is deliberately ungated: `system-statistics` is open to every
      // signed-in user and is the modal's default landing section
      // (settingsModalStore initialState / Navbar.svelte). Only the maintenance rows
      // are admin-gated.
      title: $t('settings.sections.system'),
      items: [
        { id: 'system-statistics' as SettingsSection, label: $t('settings.statistics.title'), icon: 'chart', cap: 'system.hardware_stats' },
        ...(isAdmin ? [
          { id: 'admin-task-health' as SettingsSection, label: $t('settings.taskHealth.title'), icon: 'health', cap: 'admin.task_health' },
          { id: 'search-indexing' as SettingsSection, label: $t('settings.searchIndexing.title'), icon: 'search', cap: 'admin.search_indexing' },
          { id: 'data-integrity' as SettingsSection, label: $t('settings.dataIntegrity.title'), icon: 'shield', cap: 'admin.data_integrity' },
          { id: 'embedding-migration' as SettingsSection, label: $t('settings.embeddingMigration.title'), icon: 'database', cap: 'admin.embedding_migration' },
          { id: 'retention' as SettingsSection, label: $t('settings.retention.title'), icon: 'clock', cap: 'admin.retention' },
          { id: 'backup' as SettingsSection, label: $t('settings.backup.title'), icon: 'database', cap: 'admin.backup' }
        ] : [])
      ]
    }
  ]
```

**Four things to check after pasting**, because they are the easy silent regressions:

1. `badge: pendingApprovalCount` survives on `admin-users` — it is the only `badge` in the array
   and the pending-approvals queue depends on it.
2. `isCloudEdition ? cloudTitle : title` survives on `organization-context`.
3. Every `cap:` string is unchanged from the before table. A dropped `cap` silently *shows* a row
   on a deployment that lacks the capability.
4. The two `...(isAdmin ? ...)` spreads formerly at `:304-305` are **gone** — `engine-settings`
   now inherits the group's `isAdmin` gate, and `redaction-policy` carries its own row-level one.
   Leaving a duplicate spread compiles fine and does nothing, which is how it survives review.

### Edit 2 — `SettingsModal.svelte` markup: **no change required**

`:767-811` (desktop) and `:816-846` (mobile `<optgroup>`) iterate `sidebarSections` generically.
The classes `.settings-sidebar`, `.sidebar-section`, `.section-heading`, `.section-nav`,
`.nav-item`, `.nav-item-label`, `.nav-badge`, `.lock-indicator`, `.dirty-indicator` all stay.

### Edit 3 (small, in scope) — `SettingsModal.svelte:1351-1360` — fix the dead first-group rule

`.sidebar-section:first-child` (`:1357`) **never matches**: the sidebar's first child is
`<div class="settings-search">` (`SettingsSearch.svelte:83`), so every group — including the top
one — renders a `border-top`, putting a stray divider directly under the search box. Pre-existing,
but this regroup changes which group sits there, so fix it with an adjacent-sibling rule rather
than a negation:

```css
  .sidebar-section {
    margin-bottom: 0.25rem;
    padding-top: 0.75rem;
  }

  /* The divider belongs BETWEEN groups. `:first-child` could never match here —
     `.settings-search` is the sidebar's first child — so the top group used to
     render a stray rule directly under the search box. */
  .sidebar-section + .sidebar-section {
    border-top: 1px solid var(--border-color);
  }
```

Delete the now-dead `.sidebar-section:first-child { … }` block. Confirm in **both** themes: the
border colour is `var(--border-color)`, which differs between light and dark.

### Edit 4 — `frontend/src/lib/i18n/locales/*.json` — **all 12** (ar de en es fr it ja ko nl pt ru zh)

Locale files are **flat, dot-notation JSON** (`"settings.sections.system": "System"`), not nested.
`scripts/check-i18n-parity.mjs` treats `en.json` as the reference and fails on **any** missing or
extra key in any other file, so all 12 must be edited in the same commit.

**4a — ADD 3 keys** (place them beside the existing `settings.sections.*` block — `en.json:60-66`,
`ar.json:60-66`; in `de`/`es`/`fr` the block sits around `:1738`):

| Key | en |
|---|---|
| `settings.sections.transcription` | `Transcription` |
| `settings.sections.aiChat` | `AI & Chat` |
| `settings.sections.privacyRedaction` | `Privacy & Redaction` |

**Every locale gets a REAL translation — never the English string copy-pasted.** `check:i18n`
enforces key *parity only*; a key present in all 12 with English text passes the gate and ships
untranslated (that is exactly how PC7 happened). Reference values, derived from each file's own
existing `settings.sections.transcriptionAi` / `mediaOutput` wording so conjunction style (`&` vs
the localized word) stays consistent per file:

| Locale | `…transcription` | `…aiChat` | `…privacyRedaction` |
|---|---|---|---|
| ar | التفريغ النصي | الذكاء الاصطناعي والدردشة | الخصوصية والحجب |
| de | Transkription | KI & Chat | Datenschutz & Schwärzung |
| en | Transcription | AI & Chat | Privacy & Redaction |
| es | Transcripción | IA y Chat | Privacidad y Censura |
| fr | Transcription | IA & Discussion | Confidentialité & Expurgation |
| it | Trascrizione | IA e chat | Privacy e redazione |
| ja | 文字起こし | AI & チャット | プライバシー & マスキング |
| ko | 전사 | AI 및 채팅 | 개인정보 및 마스킹 |
| nl | Transcriptie | AI & chat | Privacy & redactie |
| pt | Transcrição | IA & Conversa | Privacidade & Censura |
| ru | Транскрипция | ИИ и чат | Конфиденциальность и маскирование |
| zh | 转录 | AI & 对话 | 隐私与脱敏 |

**4b — DELETE 3 keys from all 12:**

- `settings.sections.transcriptionAi` — the group is gone (Edit 1). Only referenced at
  `SettingsModal.svelte:300` today; `rg -n "sections\.transcriptionAi" frontend/src` must return
  **only** locale files before you delete, and nothing after.
- `settings.sections.systemManagement` — folded into `System`. Same check against `:281`.
- `settings.sections.userSettings` — **already dead** before this change (PC4), zero component
  references. Removing it is the reason this plan touches it at all.

**4c — TRANSLATE the 7 English-shipping redaction labels** (PC7) — these are the two rows the new
group places side by side:

| Locale | `settings.contentRedaction.title` | `settings.redactionPolicy.title` |
|---|---|---|
| de | Inhaltsschwärzung | Schwärzungsrichtlinie |
| es | Censura de Contenido | Política de Censura |
| fr | Expurgation du Contenu | Politique d'Expurgation |
| ja | コンテンツマスキング | マスキングポリシー |
| pt | Censura de Conteúdo | Política de Censura |
| ru | Маскирование контента | Политика маскирования |
| zh | 内容脱敏 | 脱敏策略 |

(ar, it, ko, nl are already translated — leave them.)

**Do not reorder or reformat the rest of any locale file.** They are shared edit surfaces across
every v0.6.0 lane; a whole-file rewrite turns a 3-line change into a 12-way merge conflict.
⚠️ A commit touching **only** locale `.json` files does **not** fire the `frontend-check`
pre-commit hook (its `types_or` excludes `json`) — run `npm run check:i18n` by hand.

### Edit 5 — `frontend/src/components/settings/CLAUDE.md` — correct the selector list (PC3)

Under **Gotchas**, replace:

> - **E2E-guarded selectors** in the shell: `.settings-modal`, `.settings-sidebar`, `.nav-item`,
>   `.section-title`. Renaming them breaks Playwright tests — keep them stable.

with:

> - **E2E-guarded selectors** in the shell: `.settings-modal`, `.settings-sidebar`,
>   `.sidebar-section` (the group wrapper —
>   `backend/tests/e2e/test_settings_modal.py`'s `NAV_GROUPS`), `.nav-item`, and
>   `.settings-content .section-title` (the **content panel's** `<h3>`). Renaming any of them
>   breaks Playwright tests — keep them stable. Note the sidebar **group title** is
>   `.section-heading`, which is *not* guarded and is a different element from `.section-title`.

Then add, under **Privilege gating (one source of truth)**:

> - **Sidebar grouping is by WHO changes it and HOW OFTEN, not by subject** (issue #861), with
>   rows inside a group ordered by expected frequency of use. Keep groups at ~6 rows or fewer; if
>   a group outgrows that, split it rather than appending. A group that needs a privilege carries
>   `...(isAdmin ? [...] : [])` around the whole group; a privileged row inside an **ungated**
>   group carries the same spread on the row, so the group stays visible to everyone else.

### Edit 6 — `backend/tests/e2e/test_settings_modal.py:46-51` — make the switch test falsifiable (PC6)

```python
# Stable, per-user sections that render a `.section-title` quickly without
# waiting on heavy admin data loads. Each entry: (sidebar nav label, expected
# title text). Labels are matched as substrings of the nav-item text, so they
# must match the *rendered* English label, not the section id — the `download`
# section is labelled "URL Import Quality" (settings.download.title), and an
# entry reading "Download" matched nothing while the >= 3 floor hid it (#861).
SECTIONS_TO_SWITCH = [
    ("Profile & Security", "Profile"),
    ("Transcription Settings", "Transcription"),
    ("Recording Settings", "Recording"),
    ("URL Import Quality", "URL Import Quality"),
]
```

`expected_title` must be the `.settings-content .section-title` text for the `download` panel —
read it at `SettingsModal.svelte:977` (`$t('settings.download.title')`) and confirm against the
running app before committing.

Then raise the floor at `:202` so all four are required:

```python
        assert switched >= 4, (
            f"Expected to switch through all 4 settings sections, did {switched}"
        )
```

⚠️ **Watch this fail first.** Apply Edit 6 *alone*, run the test against the unmodified app, and
confirm it now fails at `switched == 3`. A test you have not seen red is not evidence. Use a
`git archive HEAD` tree rather than reverting files in the shared checkout:

```bash
git archive HEAD | (mkdir -p /tmp/redcheck861 && tar -x -C /tmp/redcheck861)
cp backend/tests/e2e/test_settings_modal.py /tmp/redcheck861/backend/tests/e2e/
cd /tmp/redcheck861 && ./scripts/e2e/run-e2e.sh -k test_switching_sections_renders_each   # expect RED
```

---

## 4. Keep-in-sync list

| Artifact | Path | Needs a change? |
|---|---|---|
| Settings search index | `frontend/src/lib/search/settingsSearchIndex.ts` | **No** — keyed by section `id`, and `visibleSections` carries item labels, not group titles (PC2). It *would* need one if a section were added or removed; this plan does neither. Re-read the file and confirm before concluding otherwise. |
| Search index test | `frontend/src/lib/search/settingsSearchIndex.test.ts` | No — same reason. Must stay green. |
| Modal unit test | `frontend/src/components/SettingsModal.test.ts` | No assertions on group titles or order (verified). It asserts lock/omit behaviour via `.settings-sidebar .nav-item` (`:108`) and `.settings-content .section-title` (`:102`). **Must stay green — it is the regression test for "who can see what."** |
| E2E — settings | `backend/tests/e2e/test_settings_modal.py` | **Yes**, Edit 6. Selectors used: `.settings-modal` (`:164`,`:173`,`:211`,`:423`), `.settings-sidebar` (`:174`), `.settings-sidebar .nav-item` (`:176`,`:185`,`:218`), `.settings-content .section-title` (`:192`,`:222`,`:399`), `NAV_GROUPS = ".settings-sidebar .sidebar-section"` (`:359`, used `:377`,`:387`,`:402`,`:421`). None depend on group membership or order. |
| E2E — others using the sidebar | `test_watch_sources_e2e.py:214,222`; `test_mfa.py:184,186`; `test_ldap_oidc.py:495,500`; `test_pki.py:91,102,194,205`; `test_language_switch.py:110` | **No** — all look rows up by **label text** inside `.settings-sidebar`, never by group. Re-run them anyway (§6). |
| Settings folder doc | `frontend/src/components/settings/CLAUDE.md` | **Yes**, Edit 5. |
| Components doc | `frontend/src/components/CLAUDE.md` | No — it only says "several components carry E2E-guarded class selectors (see each subfolder's CLAUDE.md)". Still true. |
| Frontend / root `CLAUDE.md` | `frontend/CLAUDE.md`, `CLAUDE.md` | No — neither documents the sidebar structure. |
| Section id union | `frontend/src/stores/settingsModalStore.ts` | **No — and must not change.** See §5. |
| Deep-link callers | `Navbar.svelte:107`, `FirstRunWizard.svelte:82,87`, `ChatEmptyState.svelte:37`, `MediaRecordPanel.svelte:169` | No — all pass a section **id** to `settingsModalStore.open()`. Ids are unchanged, so every deep link keeps working. |
| App-chrome gist `2d3e983d…` | (external) | Its §5.5 "Recommended regrouping" is **superseded by this file** — chiefly its `system-statistics` placement (PC5) and its "Organization" group. Leave the gist alone; note the supersession in the PR body. |
| Issue #757 body | (external) | Still contains item 2 verbatim even though #861 split it out. Worth an edit or a comment on #757 so nobody implements it twice. |

---

## 5. What must NOT change

1. **Who can see what.** The per-persona row set is identical before and after — this is the
   single most important invariant and §6 measures it. Concretely:
   - `SECTION_MIN_ROLE` (`:104-118`) is untouched. It stays the **single source of privilege
     truth**; never add an ad-hoc `&& isAdmin` to a render block.
   - **Never omit a nav row a user merely lacks the privilege for** — it renders `disabled`,
     greyed, with a padlock and a `settings.nav.requiresSuperAdmin` tooltip. Omission is
     reserved for **capabilities the deployment lacks** (`cap:`). Silently dropping a row is what
     made a field admin conclude the LDAP page did not exist.
   - **Do not move a row into a group whose gate is stricter than its own** — that omits it. The
     pairing that matters: `redaction-policy` keeps a **row-level** `isAdmin` spread inside the
     ungated *Privacy & Redaction* group (so non-admins keep seeing exactly what they see today:
     `content-redaction` and nothing else), and all six maintenance rows keep a **row-level**
     `isAdmin` spread inside the ungated *System* group.
   - Equally, **do not move a row into a LOOSER group** — dropping `redaction-policy`'s spread
     would newly show every user a padlocked super-admin row. That is a visibility change, and
     this issue is not the place to decide it (see **J-861-2**).
2. **Every `SettingsSection` id string.** `settingsModalStore.ts:3-45` is the union; the
   `dirtyState` record is keyed by it; `SECTION_NAMESPACES` in `settingsSearchIndex.ts` is keyed
   by it; five call sites deep-link by it; the content router (`:848`+) switches on it. **Renaming
   even one id is a breaking change far outside this issue's scope.**
3. **The default landing section.** `initialState.activeSection`, `open()`'s `|| 'system-statistics'`
   and `close()`'s reset (`settingsModalStore.ts:100-113`) plus `Navbar.svelte:107` all name
   `system-statistics`. Do not change them here (see **J-861-4**).
4. **E2E-guarded classes**: `.settings-modal`, `.settings-sidebar`, `.sidebar-section`,
   `.nav-item`, `.settings-content .section-title`. `.section-heading` is not currently guarded
   but keep it too — it is what carries the group title.
5. **Scope boundary — #576 and #570 stay out.** No `QuarantinePanel`, no locked-account surface,
   no placeholder rows, no disabled "coming soon" entries, no new `SettingsSection` members for
   either. This change adds **zero** sections; it only moves existing ones. See §5.1 for where
   they will land later.
6. **No panel content changes.** Every `frontend/src/components/settings/*.svelte` file is out of
   scope. If a panel looks wrong, file it; do not fix it here — the whole point of splitting #861
   out of #757 was a PR reviewable purely as an IA change.
7. **No label renames.** `download` stays "URL Import Quality" (however odd), `transcription`
   stays "Transcription Settings". Renaming rows is a separate conversation and would break the
   label-matching e2e helpers across five files. (Edit 6 adapts the *test* to the label, not the
   label to the test.)

### 5.1 Reserved slots for work landing after #861

These three v0.6.0 issues will add or modify admin settings surfaces **after** this regroup. Each
has a named home so the later implementer does not invent an eighth group. **Reserving a slot is
all this plan does — none of their content is in scope here.**

| Issue | What it adds | Reserved slot | Why there |
|---|---|---|---|
| **#570** — expose locked-account management (view + reset) | An admin view of currently-locked accounts and a reset action | **Group 7, "Administration"** — as a **tab or panel inside the existing `admin-users` row** (User Management), not a new row. Fall back to a new row **after** `admin-users` only if a tab genuinely will not fit. | It is account/identity administration, which is that group's stated rule. `admin-users` already hosts `PendingApprovalsPanel` for the approvals queue, so the "queue of accounts needing an admin decision" pattern exists there. A new top-level row would push Administration to 5 rows for a capability opened a few times a year. Whatever it becomes, add its tier to `SECTION_MIN_ROLE` — `unlock_account` is admin-tier today. |
| **#576** — quarantine/release admin UI | A `QuarantinePanel` over the three existing `/admin/files/{uuid}/quarantine`, `…/release`, `/admin/files/quarantined` endpoints | **Group 8, "System"** — a **new row** after `data-integrity`, before `embedding-migration`. | Quarantine is content-state repair on the deployment's own data (reversible, audited), which is group 8's rule — "looking at or repairing the deployment's state". It is emphatically not Administration (it configures nothing) and not a user setting. Adjacent to `data-integrity` because both operate on files rather than on configuration. It will need a `SettingsSection` id (suggest `'quarantine'`), a `SECTION_MIN_ROLE` entry (`'admin'`, matching `get_current_admin_user` on those endpoints), a `cap:` string, a `SECTION_NAMESPACES` entry in `settingsSearchIndex.ts`, and a `dirtyState` slot. |
| **#644** — discoverable local LLM base URLs | Probe-and-suggest reachable local LLM base URLs during provider setup | **Group 3, "AI & Chat"** — **inside the existing `llm-provider` row**, i.e. within `LLMSettings.svelte` / `LLMConfigModal.svelte`. **No new row, no new group.** | It is a field-level affordance on the base-URL input, not a settings section. Adding a row for it would be the exact "one concern, one row" inflation this regroup removes. |

None of the three requires a **new group**. If a later implementer believes one does, that is a
re-litigation of this plan's §2.1 principle and belongs in a comment on #861, not a silent
eighth heading.

---

## 6. Verification

Run every step. Order matters: cheap gates first, then the app, then e2e.

### 6.0 Preconditions

- **Confirm zero other writers in the checkout and a clean `git status`** before any run whose
  output you intend to treat as evidence. A concurrent pre-commit run stashes the whole tree; a
  suite can pass having tested the *committed* code. If a run's output does not mention the
  strings you just wrote (e.g. `URL Import Quality` in the e2e names), it did not test your code.
- Dev stack up via `./opentr.sh start dev` — **never** bare `docker compose`.

### 6.1 Static gates

```bash
cd frontend
npm run check          # svelte-check — a typo in a $t key will NOT be caught here; see 6.2
npm run check:i18n     # key parity across all 12 locales — MUST be run; a locale-only
                       #   commit does not fire the pre-commit hook that normally runs it
npm run lint
npm run test           # vitest: SettingsModal.test.ts + settingsSearchIndex.test.ts must stay green
npm run build          # production Vite build
```

Then, from the repo root:

```bash
scripts/safe-precommit.sh run --all-files
scripts/safe-precommit.sh run --all-files --hook-stage pre-push
```

⚠️ `prettier` **rewrites** files and then reports failure — re-stage and re-run; do not hand-fix.

### 6.2 Prove no i18n key is missing at runtime (svelte-check cannot)

A mistyped `$t('settings.sections.aiCaht')` type-checks cleanly and renders the raw key string.
With the app open (6.3), run in the browser console:

```js
[...document.querySelectorAll('.settings-sidebar .section-heading')].map(e => e.textContent.trim())
```

Every entry must be prose. Any `settings.sections.…` in the output is a missing/mistyped key.
Repeat it in at least one non-English locale.

### 6.3 Prove the visible row set did not change — the load-bearing check

Sign in at `http://localhost:5173`, open Settings (`admin@example.com` / `password` for the
admin/super-admin legs), and for **each persona** run this in the console **before** your change
(check out `aeb4dc41` or use a second browser against an unmodified build) and **after**:

```js
JSON.stringify(
  [...document.querySelectorAll('.settings-sidebar .nav-item')]
    .map(b => [b.querySelector('.nav-item-label').textContent.trim(), b.disabled])
    .sort(),
  null, 2
)
```

**The two outputs must be byte-identical** for every persona. Order differs (that is the change);
the *sorted set of (label, disabled) pairs* must not.

| Persona | Expected row count | Expected padlocked |
|---|---|---|
| Regular user (community) | 18 | 0 |
| Admin, not super (community) | 29 | 9 — `authentication`, `audit-logs`, `backup`, `engine-settings`, `redaction-policy` … confirm against `SECTION_MIN_ROLE` |
| Super admin (community) | 29 | 0 |

(Counts assume every capability enabled, which is the community default. Re-derive rather than
trusting the table if your deployment disables any `cap`.)

Also confirm the group **count and titles**:

- Regular user: 6 groups — Account, Transcription, AI & Chat, Privacy & Redaction, Media & Output,
  System (System holding exactly one row).
- Admin: 7 groups — the above plus Administration, with System holding 7 rows.

### 6.4 Screenshot matrix — capture every cell

Use `~/bin/browser-tools/browse.js` (pass `--display=:13` on XRDP) or Playwright `--headed` with
`DISPLAY=:11`. Attach all of these to the PR — the issue's deliverable is an IA judgment, and it
cannot be reviewed from a diff.

For **each** of the four states below, capture **light** and **dark** (toggle via the navbar theme
control; the app sets `data-theme` on `<html>`):

| # | Screen / state | Must show |
|---|---|---|
| 1 | Settings modal, **desktop** (≥1200px), **regular user**, sidebar scrolled to top | All 6 groups with their headings; the one-row System group at the bottom; **no stray divider directly under the search box** (Edit 3) |
| 2 | Settings modal, **desktop**, **admin (not super)** | All 7 groups; the padlock + tooltip on `redaction-policy` inside Privacy & Redaction, and on `authentication` / `engine-settings` / `audit-logs` inside Administration; the pending-approvals **badge** on User Management if the queue is non-empty |
| 3 | Settings modal, **desktop**, **super admin**, hovering a row in each of the 8 groups | Hover state renders correctly per group (guards against the `:focus` background regression of #746); every row unlocked |
| 4 | Settings modal, **mobile** (≤768px — the `.settings-sidebar` is `display:none` and `.mobile-section-nav` takes over), `<select>` **open** | The `<optgroup>` labels are the new group titles, in the new order, with locked rows disabled and suffixed `— <lock reason>`. **Nothing tests this picker — it is the highest-risk surface in the change.** |

Then, **in `ar` (RTL)** — switch language in Settings → Profile & Security → Language, and confirm
`document.documentElement.dir === 'rtl'`:

| # | Screen / state | Must show |
|---|---|---|
| 5 | Settings modal, desktop, **`ar`**, light **and** dark | All group headings in Arabic (no raw keys, no English); headings right-aligned; the `.section-heading` `letter-spacing: 0.06em` + `text-transform: uppercase` (`:1362-1371`) do not mangle Arabic script; the `.sidebar-section` divider spans correctly; the padlock/badge sit on the correct (left) side of each row |
| 6 | Settings modal, **mobile**, **`ar`**, `<select>` open | `<optgroup>` labels render RTL and are not truncated |

Spot-check one more non-Latin locale (`ja` or `zh`) and one longer-string locale (`ru` or `de` —
"Konfidenzialität…"-class strings are where a 240px sidebar wraps) at desktop light mode.

### 6.5 E2E

```bash
./scripts/e2e/run-e2e.sh -m settings
./scripts/e2e/run-e2e.sh -k "test_settings_modal or test_language_switch"
./scripts/e2e/run-e2e.sh -k "test_watch_sources_e2e or test_mfa or test_pki or test_ldap_oidc"
```

The third line covers the four other files that look rows up inside `.settings-sidebar`. `-m pki`
and the LDAP/OIDC legs need their overlays (`./opentr.sh start dev --with-pki` /
`--with-ldap-test` / `--with-keycloak-test`; Keycloak's first start takes ~10 minutes and that is
normal). If an overlay is unavailable, say so explicitly in the PR rather than reporting the leg
as passed.

Visual-regression baselines: `backend/tests/e2e/test_visual_regression.py` has a `"settings"`
entry keyed on `.settings-modal .stats-grid` — the **stats panel**, not the sidebar. Check whether
it captures sidebar pixels before assuming the baseline is unaffected; if it is, re-capture per
`project_visual_baselines_waived_v050` rather than `--force-verify`.

### 6.6 Test-honesty gates

```bash
cd frontend && npm run test:audit
cd backend && python3 ../scripts/audit-tests.py tests
```

`audit-tests.py` is a **whole-tree** gate — get to 0 open findings or the commit is blocked for
every lane in the worktree.

---

## 7. Judgment calls left open

Issue-prefixed per repo convention — nine earlier plans each numbered from `J1` and collided.

| Id | Question | Who decides | Recommendation |
|---|---|---|---|
| **J-861-1** | **Group names.** The three new titles are *Transcription*, *AI & Chat*, *Privacy & Redaction*. "Transcription" as a group heading sits above a row labelled "Transcription Settings" — tolerable (headings are 0.6875rem uppercase tertiary-colour, rows are 0.8125rem body) but not free. Alternatives considered: "Transcription & Speakers", "Speech", "Recognition". | **Owner** — the issue names the titling as the deliverable, and nobody but the product owner can settle naming. | Ship *Transcription* / *AI & Chat* / *Privacy & Redaction*. Escalate before implementing if the owner is reachable; do not silently substitute. |
| **J-861-2** | **Should `redaction-policy` lose its row-level `isAdmin` spread**, so every user sees it padlocked next to Content Redaction? `settings/CLAUDE.md` says never to omit a row a user merely lacks privilege for — by that rule the current inline `isAdmin` omission is arguably already wrong, and the new adjacency would *teach* the user/admin distinction. But it is a **visibility change**, which §5 forbids in this issue. | **Owner** — it is a policy question about admin-surface discoverability, not an implementation detail. | **Keep the spread** (no visibility change) for #861. File the question as its own issue. The same question applies to `engine-settings` and the six maintenance rows; answer it once, for all of them, not row by row. |
| **J-861-3** | **Does `engine-settings` belong in Administration or in System?** It is super-admin deployment tuning (ASR model, diarization) — "how this deployment behaves" (group 7) rather than "repair its state" (group 8). | Implementer, if the owner does not weigh in. | Administration, as specified. Either placement is defensible; what is not defensible is leaving it in a transcription group a regular user reads. |
| **J-861-4** | **Should the default landing section move off `system-statistics`?** With System last, the modal opens on the bottom group's first row. `profile` would be the natural default. | **Owner.** | **Out of scope for #861** — `Navbar.svelte:107` passes `'system-statistics'` explicitly, so this is a deliberate product decision, not a side effect of the regroup, and changing it would alter every user's first impression of Settings. File separately. |
| **J-861-5** | **Is Edit 3 (the dead `:first-child` CSS rule) in scope?** It is a pre-existing one-line cosmetic bug, not an IA change. | Implementer. | **Yes, include it.** The repo's standard is to fold an incidental finding into the branch already open rather than file it and leave it; it is three lines, in the exact block being touched, and the regroup changes which group sits under the stray divider. |
| **J-861-6** | **Icon collisions.** Seven `icon` values are duplicated across rows (`message` on `ai-prompts`+`chat`, `users` on `admin-users`+`team`, `database` on `backup`+`embedding-migration`, `mic` on `asr-provider`+`recording`, `shield` on `data-integrity`+`redaction-policy`, `user` on `profile`+`speaker-attributes`, `list` on `audit-logs`+`custom-vocabulary`). **The desktop sidebar does not render icons at all** — `:779-805` renders only `.nav-item-label`, badge and padlock — so today the field is inert data. | Implementer. | **Out of scope.** Leave every `icon` value exactly as-is. Resolving the collisions only matters if the sidebar starts rendering icons, which is #753's territory (app-chrome / icon registry). Note the inertness in the PR so #753 inherits it. |

---

## 8. Commit and PR

Two commits, in this order, so the IA change is reviewable apart from its fallout:

```
feat(ui): regroup the settings sidebar by audience and frequency

Closes #861
```
— Edits 1, 3, 4, 5.

```
test(e2e): make the settings section-switch test falsifiable
```
— Edit 6. (Its `"Download"` entry could never match the rendered label "URL Import Quality", and
the `switched >= 3` floor hid it.)

Per repo policy: **one keyword per issue** in a `Closes` line — `Closes #1, #2` closes only the
first, silently. Only #861 closes here. #757 does **not**: it retains item 1 (multi-select
parity). Consider commenting on #757 that item 2 shipped via #861, since its body still contains
item 2 verbatim.

PR body should carry: the §2.2 after-table with reasoning (that *is* the deliverable), the §2.3
before/after metrics, the §6.3 identical-row-set evidence for all three personas, and the §6.4
screenshot grid including `ar` RTL. Open it against the branch's actual upstream
(`git rev-parse --abbrev-ref @{u}`), not an assumed `master`.
