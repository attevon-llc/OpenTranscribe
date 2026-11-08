# Legal holds: usability, freeze semantics and documentation (v0.7.0)

Status: plan, written before the code (planning lane `v070/legal-hold-plan`, based on `14c4259c0`).
Owner decisions of 2026-11-07 are binding; this file turns them into lanes a fresh implementation
agent can execute without this conversation. Nothing here is implemented yet.

Read first: `CLAUDE.md` (root), `backend/CLAUDE.md`, `backend/app/services/CLAUDE.md` (the
**Holds** bullet), `backend/app/api/CLAUDE.md`, `backend/app/models/CLAUDE.md`,
`backend/app/db/CLAUDE.md`, `backend/alembic/CLAUDE.md`, `frontend/CLAUDE.md`,
`frontend/src/components/settings/CLAUDE.md`, `frontend/src/components/gallery/CLAUDE.md`,
`docs-site/CLAUDE.md`, `docs/design/1091_data_safety_cluster_plan.md` §4 (precedence),
`docs/design/1336_identity_history_plan.md` (speaker identity history).

---

## 0. Decisions this plan implements (owner, 2026-11-07)

| # | Decision | Where it lands |
|---|---|---|
| D1 | Only admins place/release holds. Managed edition: organization admins (existing `/org-admin/holds`). Community edition: platform admins can hold files or collections **without** quarantining them. Regular users never place/release and never see the reason; they see the badge and the explainer. | Lane B (operator hold), Lane D (UI gating) |
| D2 | A hold **freezes** the file's content in its existing state. Read, search, playback, export and download stay. New comments allowed (audited annotations); existing comments frozen. Every content write path goes through `hold_guard`, policed by one structural test. Transcript edits on held files change from "allowed + `content.transcript.edit`" to "refused + `content.hold.refused`". | Lane A |
| D3 | Easy placement: "Place legal hold" bulk action in the gallery selection (admins), a hold action on the file detail page, a "Copy file ID" control, the existing paste-IDs / collection path kept. Release stays in Settings > Legal holds. **Amended 2026-11-08 (owner feedback):** the file-page hold action and header Copy file ID are removed, and Settings picks files by title or file name (`ui/FileSearchPicker`) instead of pasted IDs; the file ID lives in File details. | Lane D |
| D4 | Held files appear under a system "Legal hold" tag and a system "Legal hold" collection, automatically, removed when the last hold releases; not editable, not deletable, per tenant, no reason, no cross-tenant leak. | Lane C (server), Lane D (UI). Design choice: **computed (virtual), not rows**, §5 |
| D5 | Plain-language explanation: help text, info-icon tooltip, expandable "What does a legal hold do?" on the hold panel, the hold dialog and the held-file banner; 12 locales; RTL. | Lane D |
| D6 | Open-source docs: user guide for admins and users, hold / quarantine / trash / delete, the freeze rules, the community operator hold. Managed-edition doc needs listed separately (§11). | Lane E |
| D7 | Precedence unchanged: hold > trash purge > retention; GDPR erasure deferred while held (Art. 17(3)(e)). | All lanes (no lane may weaken it), §3.6 |

---

## 1. Research basis for "freeze" (why freezing, not versioning)

- **FRCP 37(e)** (as amended 2015): when electronically stored information that should have been
  preserved in the anticipation or conduct of litigation is lost because a party failed to take
  **reasonable steps** to preserve it, and it cannot be restored or replaced, the court may order
  curative measures, and on a finding of intent to deprive, adverse-inference instructions or
  dismissal. A transcript, its speaker attributions and its edit state are ESI.
- **The Sedona Conference, Commentary on Legal Holds, Second Edition: The Trigger & The Process**
  (2019): once the duty to preserve attaches, a party suspends routine disposition and preserves
  relevant information **in a reasonable, good-faith, proportionate** way; the hold should be
  documented, communicated, and its scope and release recorded. This matches the existing
  `content_hold` shape (reason, matter reference, placed/released by, release reason).
- **Zubulake v. UBS Warburg** ("Zubulake IV", S.D.N.Y. 2003): once litigation is reasonably
  anticipated, a party must suspend routine destruction and put a litigation hold in place.
- **Microsoft Purview eDiscovery holds** show the alternative model: users keep editing and
  deleting, and the platform silently retains the original and every prior version (Recoverable
  Items / Preservation Hold library). That is defensible **only because versions are kept**.
- **OpenTranscribe keeps no transcript versions** (issue #46, transcript version control, is open
  and unbuilt). An edit to a held file overwrites the only copy of the pre-hold text, speaker
  name, title or comment. The `content.transcript.edit` audit event records THAT an edit happened
  (ids and field names, never the old value), so it cannot restore what was lost: under FRCP 37(e)
  it documents the loss rather than preventing it. Freezing is therefore the only way to
  "preserve information in its existing state" with what exists today.
- **Revisit when #46 ships:** with append-only versions, the Purview model (allow edits, preserve
  every version) becomes possible and is friendlier. Leave a note in `hold_guard`'s module
  docstring pointing at #46.
- **GDPR Art. 17(3)(e)**: the right to erasure does not apply to the extent processing is
  necessary for the establishment, exercise or defence of legal claims. Existing behaviour
  (erasure deferred while held, retried on release via `notify_hold_released`) stays.

Lane E puts a short, citation-light version of this in the user docs (no pin cites in product
docs; link to Sedona's public page and the FRCP rule text).

---

## 2. Verified current state (master at `14c4259c0`)

### 2.1 Backend

| Fact | Where |
|---|---|
| Alembic head is `v464_generalise_erasure_grace_index`; `v465`, `v466` are reserved by the data-safety lanes (`backend/alembic/CLAUDE.md:22`). **This plan uses `v467`.** | `python3 scripts/release-tests/lib/alembic-head.py backend` |
| Organization hold tables `content_hold`, `content_hold_file`. `organization_id` is **NOT NULL** (`content_hold.py:80-82`), so today a hold can only be an organization's. Collection membership listener covers files added later (`content_hold.py:131-151`). | `backend/app/models/content_hold.py` |
| "Is this file held?" lives in ONE module: `hold_state(s)`, `held_pred`, `not_held_pred`, `is_held`, `assert_not_held`, `refusal_detail`, `LEGAL_HOLD_ERROR_CODE = "FILE_UNDER_LEGAL_HOLD"`. `HoldState(platform: bool, org_hold_uuids)`; `platform` = the `media_file.legal_hold` flag. `_active_hold_covers_file` (`hold_guard.py:113-119`) matches **any** active `content_hold`, not only organization ones. | `backend/app/services/hold_guard.py` |
| `REPROCESS_REFUSAL` (`hold_guard.py:54-57`) and `DESTROY_DERIVED_REFUSAL` (`:61-64`) both say "Editing the transcript is still allowed." That becomes false (Lane A rewrites both). | same |
| Place/release service, never commits; `record_hold_change` stages `content.hold.place/release` in the outbox; `finish_hold_change` applies storage object lock and erasure nudge. `record_edit_on_held_file` (`content_hold_service.py:276-334`) stages `content.transcript.edit` for edits to held files. `record_hold_refusal` (`:337-358`) writes `content.hold.refused` best-effort. | `backend/app/services/content_hold_service.py` |
| Org routes: `GET/POST /api/org-admin/holds`, `GET /{uuid}`, `POST /{uuid}/release`; `require_org_admin`; capability `org.legal_hold` (default **False** in community, `core/capabilities.py:164`), `platform_admin_bypass=False`, `refuse_under_grant=True` (`api/router.py:287-296`). | `backend/app/api/endpoints/org_admin_holds.py` |
| Reads for the badge: `file_hold_summary` (`content_hold_queries.py:74-81`), `collection_hold_summaries` (`:84-110`) hard-codes `held_by=[HELD_BY_ORGANIZATION]`. `list_holds` filters `organization_id == org_id`. | `backend/app/services/content_hold_queries.py` |
| Platform hold today = the takedown's `media_file.legal_hold` flag, set only by `POST /api/admin/files/{uuid}/quarantine` (`admin_quarantine.py:142-184`, `legal_hold` default on) and cleared by `/release` (`:187-227`, `clear_legal_hold`). There is **no** way to hold without quarantining. | `backend/app/api/endpoints/admin_quarantine.py`, `services/takedown_service.py:217,403` |
| Only `hold_guard`, the takedown flow and the listed schemas may read `.legal_hold` (`tests/unit/test_hold_guard_single_source.py`, AST scan, allowlist with reasons). | test |
| `hold` (a `HoldSummary`) is on `MediaFileDetail` only (`schemas/media.py:229-231`); the gallery list schema has no hold field, so gallery cards never show the badge. | `backend/app/schemas/media.py` |
| Gallery list: `GET /api/files` (`files/listing.py:134`) → `apply_all_filters` (`files/filtering.py:519`); a collection's files come from `GET /api/collections/{uuid}/media`; facet metadata from `get_metadata_filters` (`filtering.py:561`). | |
| Tags: system tags exist (`tag.user_id IS NULL AND organization_id IS NULL`, seeded "Important/Meeting/Interview/Personal", visible in every tenant, `models/tag.py:28-45`). `file_tag` has no per-user column: a link is global to the file. Collections have **no** system kind (`models/collection.py`; CHECK `user_id IS NOT NULL OR organization_id IS NOT NULL`). | |
| File-tag writes go through `services/tag_files.py:16 add_file_tag` / `:35 remove_file_tag` (4 call sites). Collection memberships are inserted at `media_collections_membership.py:165`, `files/prepare_upload.py:167`, `services/auto_label_service.py:526`. | |
| Redaction is a read-time mask over cached spans; the original transcript is never rewritten (`services/redaction/CLAUDE.md`). Spans change only on a segment text edit, an explicit rescan, or a model-version re-detect. | |
| Speaker identity history (#1336, `v462`) is appended by a flush listener over `Speaker` tracked fields (`utils/speaker_identity_history.py`, `models/speaker_identity_event.py`). Under a freeze no tracked field of a held file's speaker changes, so no event is written; nothing to change there. | |
| Precedence (1091 §4): holds win over everything that destroys; a held file in the trash stays trashed, restorable, never purged. | `docs/design/1091_data_safety_cluster_plan.md:573-590` |

### 2.2 Frontend

| Fact | Where |
|---|---|
| `HoldBadge.svelte` renders a "Legal hold" pill with a hover title by authority; used in `FileHeader.svelte:159` and `collections/CollectionsList.svelte`. | `frontend/src/components/ui/HoldBadge.svelte` |
| `OrgLegalHoldsPanel.svelte` (521 lines): list, place (collection or pasted UUIDs, max 500, reason 10-2000, matter ref), release. Shown under Settings when `orgAdminCapOn(capState, 'org.legal_hold')` (`SettingsModal.svelte:438,1547`), loaded via `lib/settings/sectionLoaders.ts:42`. | |
| 50 `holds.*` keys in each locale; `HOLD_REFUSAL_KEYS` / `HOLD_BADGE_TITLE_KEYS` in `lib/i18n/keyMaps.ts:334-349`. The badge/refusal strings say "cannot be deleted or reprocessed" only. | |
| Gallery selection toolbar `gallery/GallerySelectionActions.svelte` (685 lines) has `isAdmin` (`role` admin/super_admin) and the quarantine action wired in `routes/+page.svelte:757-771` (`QuarantineModal`). | |
| File detail page `routes/files/[id]/+page.svelte` with `FileHeader.svelte`; no "copy file ID" control exists. | |

### 2.3 Docs

- `docs-site/docs/user-guide/admin-panel.md:415-452` "Organization legal holds" (says "Editing a
  transcript is still allowed", and records `content.transcript.edit`: both become false).
- `docs/abuse-and-takedown.md:276-301` "Hold, quarantine and organization hold" table: row
  "Transcript edits: Allowed" becomes "Refused"; no column for an operator hold without takedown.
- `docs-site/docs/operations/upgrading-to-0-7-0.md` is where behaviour changes go.

---

## 3. Freeze semantics (normative)

### 3.1 What "held" freezes

A file is **frozen** when `hold_guard` says it is held (platform flag, platform hold or
organization hold) **and** its initial processing run has finished
(`status NOT IN ('pending', 'processing')`). Rationale: a hold placed on a file still being
transcribed preserves the content as of the end of that run; the pipeline that is already
running is not "a change to existing content". Anything dispatched after the run ends (automatic
summary, topic extraction, voice match, auto-label) is a separate write and is refused or
skipped. Route guards do not need the status nuance: a user cannot edit a file that is still
processing; they refuse whenever `held`.

| Frozen (refused with `409 FILE_UNDER_LEGAL_HOLD`, or skipped by a sweep) | Still allowed |
|---|---|
| Transcript segment text and times | Viewing, searching, playback, streaming |
| Segment speaker reassignment | Export (all formats), document export, subtitle export, bulk export, download |
| Speaker create / rename / delete / merge / link to or unlink from a profile / verify / reject / gender confirm, on the held file | **New** comments (a separate, audited annotation: `content.hold.annotation`) |
| Title, filename, description, recorded date, language and every other `PUT /files/{uuid}` field | Restoring from the trash a file that was trashed before the hold (visibility, not content; 1091 §4). Moving a held file **to** the trash stays refused (destroy path, unchanged) |
| Summary generate / regenerate / retry / delete | Search-index rebuilds, re-embedding, waveform / thumbnail / analytics / video cache regeneration (derived copies of the frozen record) |
| Topic extraction, topic apply, suggestion dismiss, AI auto-label | Collection/tag **rename**, collection visibility and shares (they change labels and access, not the file's record) |
| Redaction rescan (explicit) and the redaction reindex sweep | The lazy first redaction scan of a file that has never been scanned (§3.4) |
| Adding/removing tags on the file, merging/deleting a tag that a held file carries | Placing more holds; releasing a hold (admins) |
| Adding the file to / removing it from any collection; deleting a collection that contains a held file | |
| Editing or deleting existing comments | |
| Reprocess, retry, recover, rediarize (already refused today) | |
| Voiceprint consent change (already refused today) | |

### 3.2 Refusal shape

Unchanged wire contract: HTTP 409, `detail = refusal_detail(state, reveal_uuids=..., message=...)`
= `{"error": "FILE_UNDER_LEGAL_HOLD", "message": ..., "held_by": [...], "holds"?: [...]}`. New
message constant `hold_guard.FREEZE_REFUSAL`:

> refused: the file is under an active legal hold, so its content is preserved exactly as it was.
> You can still read, search, play, export and download it, and add a comment. An administrator
> must release the hold before it can be changed.

`REPROCESS_REFUSAL` and `DESTROY_DERIVED_REFUSAL` drop "Editing the transcript is still allowed".
Bulk paths return the same code per file in `BulkActionResult.error` (existing pattern,
`bulk_rework_actions.py:22-29`).

### 3.3 What the audit records instead

- Today: an edit to a held file commits and stages `content.transcript.edit` (`kind`, `fields`,
  `segment_uuid`/`speaker_uuids`, `held_by`).
- After Lane A: the edit is refused and `content.hold.refused` is written through
  `record_hold_refusal` with `details = {"action": <action>, "held_by": [...]}` (never the reason,
  never text). Action vocabulary (closed set, one constant tuple `hold_guard.FREEZE_ACTIONS`, test
  pins it): `segment_edit`, `segment_speaker`, `speaker_merge`, `speaker_create`,
  `speaker_update`, `speaker_delete`, `speaker_link`, `speaker_verify`, `speaker_gender`,
  `file_update`, `summary_generate`, `summary_delete`, `identify_speakers`, `topic_extract`,
  `topic_apply`, `topic_dismiss`, `auto_label`, `redaction_rescan`, `tag_add`, `tag_remove`,
  `tag_merge`, `tag_delete`, `collection_add`, `remove_from_collection`, `delete_collection`,
  `comment_edit`, `comment_delete`, `reprocess`, `retry`, `recover`, `profile_delete`, plus the
  existing ones (`delete`, `cancel_upload`, `summary_delete`, `speaker_delete`). Existing refusal
  action strings are kept verbatim (they are already in tenants' audit logs).
- `content.hold.refused` stays best-effort (`record_content_event`, written outside the request
  transaction, so it survives the refusal's rollback).
- `AuditEventType.CONTENT_TRANSCRIPT_EDIT` **stays in the enum** (historic rows must stay readable
  and filterable, `keyMaps.ts` maps it) but is no longer emitted; its docstring says "emitted
  before v0.7.0 for edits to held files; a held file now refuses edits".
  `record_edit_on_held_file` is **deleted** with its three call sites (no second path).
- New `AuditEventType.CONTENT_HOLD_ANNOTATION = "content.hold.annotation"`: a comment created on a
  held file, staged through the outbox **in the comment's own transaction**
  (`stage_content_event`), details `{"comment_uuid", "held_by"}`; not in `GATED_EVENTS`.
  Add the i18n label (12 locales) wherever audit event labels are mapped.

### 3.4 Redaction nuance

Masking is a read-time view; the stored transcript never changes. Two writes exist:
1. An **explicit rescan** (bulk action `redact`, `bulk_action_handlers.py:238/379`) and the
   **admin reindex sweep** (`redaction_settings.py:279` → `tasks/redaction_task.py:180`)
   replace cached spans, which changes what a held file displays and exports. Frozen: refused /
   skipped via `not_held_pred()`.
2. The **lazy first scan** of a file that has never been scanned (`redaction_gate`, on transcript
   read when masking is mandated). Allowed: refusing it would withhold the held file's
   transcript forever where masking is forced, which defeats "keep reading". Pinned by a test.

### 3.5 Cross-file operations

Speaker **profiles**, **clusters** and **tags** span many files. Rule: an operation that would
write a frozen field of a held file's row is either **refused** (it would destroy or re-point
held data: profile delete, tag delete, tag merge, collection delete with held members) or
**excludes the held rows** and proceeds (profile rename propagation, cluster promote / merge /
split / unassign / batch-verify). Exclusion uses `not_held_pred()` inside the set-based write,
and the response gains an additive `held_skipped: int` where the route already returns a dict.
A profile rename therefore leaves held files' speakers with the name they had when held.

### 3.6 Precedence (D7, unchanged)

hold > trash purge > retention. A held file cannot be trashed; a held file already in the trash
stays there, restorable, never purged; release makes an overdue trashed file eligible on the next
tick (1091 §4.1). GDPR erasure, account deletion and organization erasure defer held files
(Art. 17(3)(e)); release nudges `notify_hold_released`. A platform hold (Lane B) is a
`content_hold` row, so `_active_hold_covers_file` already includes it in every destroy predicate:
**no destroy path needs a change for Lane B**, and Lane B adds a test proving it.

Edge to verify in Lane A (finding, fix in the same lane): account deletion of a user who
commented on **someone else's** held file. `Comment.user_id` is `NOT NULL` (`models/media.py:633`),
so erasure must delete or de-attribute that comment. Under the freeze that is a comment delete on
a held file. Expected handling: account erasure defers that comment with the rest of the held
data (the existing `held_pred` deferral of the user's own files does not cover it). Write the
failing test first, then route the comment cleanup through `not_held_pred()` + the deferral
ledger.

---

## 4. Write-path inventory (every write that touches a held file's content)

Line numbers are the route function's `def` line on `14c4259c0`. "Today" says what a held file
gets now. Every row becomes a row in the structural test's table (§6). Prefixes from
`api/router.py`.

### 4.1 API routes that must refuse (new guard)

| # | Route | Handler | Today | After |
|---|---|---|---|---|
| W1 | `PUT /api/files/{uuid}/transcript/segments/{seg}` | `files/__init__.py:229` → `files/crud.py:316 update_single_transcript_segment` | allowed + `content.transcript.edit` (`crud.py:399`) | refuse `segment_edit` |
| W2 | `PUT /api/transcripts/segments/{seg}/speaker` | `transcript_segments.py:251` | allowed + audited (`:337`) | refuse `segment_speaker` |
| W3 | `POST /api/speakers/{s}/merge/{t}` | `speakers_merge.py:40` | allowed + audited (`:123`) | refuse `speaker_merge` (either side held) |
| W4 | `PUT /api/speakers/{uuid}` | `speakers.py:289` | allowed | refuse `speaker_update` |
| W5 | `POST /api/speakers` | `speakers.py:64` | allowed | refuse `speaker_create` |
| W6 | `POST /api/speakers/{uuid}/verify` | `speakers_suggestions.py:51` | allowed | refuse `speaker_verify` |
| W7 | `POST /api/speakers/{uuid}/confirm-gender` | `speakers_suggestions.py:123` | allowed | refuse `speaker_gender` |
| W8 | `POST /api/speaker-profiles/speakers/{uuid}/assign-profile` | `speaker_profiles_matching.py:83` | allowed | refuse `speaker_link` |
| W9 | `PUT /api/files/{uuid}` | `files/single_file.py:153` → `files/crud.py:152 update_media_file` | allowed | refuse `file_update` |
| W10 | `POST /api/files/{uuid}/summarize` | `summarization.py:75` | allowed | refuse `summary_generate` |
| W11 | `POST /api/files/{uuid}/identify-speakers` | `summarization.py:517` | allowed | refuse `identify_speakers` |
| W12 | `POST /api/files/{uuid}/retry-summary` | `files/summary_status.py:108` | allowed | refuse `summary_generate` |
| W13 | `POST /api/files/{uuid}/extract` (topics) | `topics.py:390` | allowed | refuse `topic_extract` |
| W14 | `POST /api/files/{uuid}/apply` (topics) | `topics.py:472` | allowed | refuse `topic_apply` |
| W15 | `DELETE /api/files/{uuid}/suggestions` | `topics.py:536` | allowed | refuse `topic_dismiss` |
| W16 | `POST /api/files/{uuid}/auto-label` | `topics.py:251` | allowed | refuse `auto_label` |
| W17 | `POST /api/files/batch-extract` | `topics.py:62` | allowed | per-file skip, report held count |
| W18 | `POST /api/files/retroactive-auto-label` | `topics.py:222` | allowed | task excludes held (T4) |
| W19 | `POST /api/tags/files/{uuid}/tags` | `tags/crud.py:200` | allowed | refuse `tag_add` (guard in `services/tag_files.py:16`) |
| W20 | `DELETE /api/tags/files/{uuid}/tags/{name}` | `tags/crud.py:248` | allowed | refuse `tag_remove` (guard in `tag_files.py:35`) |
| W21 | `POST /api/tags/{uuid}/merge` | `tags/operations.py:147` | allowed | refuse `tag_merge` when a source tag is on a held file |
| W22 | `DELETE /api/tags` | `tags/operations.py:183` | allowed | refuse `tag_delete` when a tag is on a held file |
| W23 | `POST /api/tags/promote` | `tags/operations.py:69` → `services/tag_operations.py promote_tags_to_shared` | allowed | verify whether it re-points `file_tag` rows; if so refuse `tag_merge` for held files, else classify exempt with that reason |
| W24 | `POST /api/collections/{uuid}/media` | `media_collections_membership.py:106` (insert `:165`) | allowed | refuse `collection_add` for held files |
| W25 | `DELETE /api/collections/{uuid}/media` | `media_collections_membership.py:203` | refused only if the **collection** is held (`:245`) | refuse `remove_from_collection` for any held file |
| W26 | `DELETE /api/collections/{uuid}` | `media_collections.py:373` | refused only if the collection is held (`:400`) | also refuse `delete_collection` when any member is held |
| W27 | `PUT /api/comments/{uuid}` | `comments.py:286` | allowed | refuse `comment_edit` |
| W28 | `DELETE /api/comments/{uuid}` | `comments.py:337` | allowed | refuse `comment_delete` |
| W29 | `POST /api/files/management/bulk-action` `summarize` | `bulk_actions.py:202` → `bulk_action_handlers.py:160` | allowed | per-file `FILE_UNDER_LEGAL_HOLD` |
| W30 | same, `identify_speakers` | `bulk_action_handlers.py:191` | allowed | per-file refusal |
| W31 | same, `redact` | `bulk_action_handlers.py:379` → `:238` | allowed | per-file refusal (§3.4) |
| W32 | same, `add_tag` / `remove_tag` | `bulk_action_handlers.py:300` → `services/tag_bulk.py:170,175` | allowed | per-file refusal (via `tag_files` guard) |
| W33 | `POST /api/files/{uuid}/recover` | `files/management.py:321` | backstop only (`utils/file_retry.py:188` logs and returns False) | explicit 409 `recover` up front |
| W34 | `POST /api/tasks/retry/{uuid}` | `tasks_file_repair.py:127` | backstop only | explicit 409 `retry` |
| W35 | `POST /api/my-files/{uuid}/retry` | `user_files.py:381` | backstop only | explicit 409 `retry` |
| W36 | `PUT /api/speaker-profiles/profiles/{uuid}` | `speaker_profiles.py:365` | propagates new name to every linked speaker | exclude held-file speakers (§3.5) |
| W37 | `DELETE /api/speaker-profiles/profiles/{uuid}` | `speaker_profiles.py:497` (unlinks `:532`) | allowed | refuse `profile_delete` when a linked speaker is on a held file |
| W38 | `POST /api/speaker-clusters/batch-verify` | `speaker_clusters.py:187` | allowed | exclude held-file speakers |
| W39 | `POST /api/speaker-clusters/{uuid}/unassign` | `speaker_clusters.py:384` | allowed | exclude held-file speakers |
| W40 | `PUT /api/speaker-clusters/{uuid}` | `speaker_clusters.py:429` | allowed | exclude held-file speakers from any name propagation |
| W41 | `DELETE /api/speaker-clusters/{uuid}` | `speaker_clusters.py:476` (bulk `cluster_id = NULL` `:510`) | allowed | `cluster_id` is cross-file grouping, not the record: exempt **unless** it also unlinks profiles; implementer verifies and records which |
| W42 | `POST /api/speaker-clusters/{uuid}/promote` | `speaker_clusters.py:531` | links members to a profile | exclude held-file speakers |
| W43 | `POST /api/speaker-clusters/{s}/merge/{t}` | `speaker_clusters.py:564` | allowed | exclude held-file speakers from relinking |
| W44 | `POST /api/speaker-clusters/{uuid}/split` | `speaker_clusters.py:602` | allowed | exclude held-file speakers from relinking |
| W45 | `POST /api/admin/redaction-policy/reindex` | `redaction_settings.py:279` | rescans all | task skips held (T5) |

### 4.2 API routes already refusing (keep; migrate to the shared helper)

| # | Route | Guard today |
|---|---|---|
| W46 | `DELETE /api/speakers/{uuid}` (`speakers.py:424`) | `refuse_reprocess_if_held` `:459` |
| W47 | `DELETE /api/files/{uuid}/summary` (`summarization.py:603`) | `:630` |
| W48 | `POST /api/files/{uuid}/reprocess` (`files/__init__.py:259`) | `reprocess.py:280` |
| W49 | `POST /api/files/{uuid}/retry` (`files/management.py:214`) | `:247` |
| W50 | bulk `reprocess` / `retry` (`bulk_rework_actions.py:106/33`) | `:26` |
| W51 | `PATCH /api/files/{uuid}/voiceprint-consent` (`voiceprint_consent.py:60`) | `:80` |

### 4.3 Allowed write on a held file (with audit)

| # | Route | After |
|---|---|---|
| W52 | `POST /api/comments/files/{uuid}/comments` (`comments.py:85`) | allowed; stage `content.hold.annotation` in the comment's transaction |
| W53 | `POST /api/comments` (`comments.py:161`) | same |

### 4.4 Background tasks

| # | Task | After |
|---|---|---|
| T1 | `tasks/summarization.py:435 summarize_transcript_task` | at start: if frozen, mark `summary_status` unchanged, return `{"status": "skipped_legal_hold"}`; no write |
| T2 | `tasks/topic_extraction.py:273 extract_topics_task`, `:439 batch_extract_topics_task` | skip frozen files |
| T3 | `tasks/speaker_identification_task.py:600 identify_speakers_llm_task` | skip frozen |
| T4 | `tasks/auto_labeling.py:55 group_batch_files_task`, `:161 retroactive_auto_label_task`, `:278 auto_label_batch_task` (membership insert `services/auto_label_service.py:526`) | exclude frozen files |
| T5 | `tasks/redaction_task.py:72 redaction_detect_task` (refuse a **rescan** of a frozen file; allow a first scan), `:180 redaction_reindex_all_task` (`not_held_pred()`) | §3.4 |
| T6 | `tasks/speaker_update_task.py:166`, `tasks/speaker_merge_task.py:90`, `tasks/speaker_embedding_reassignment_task.py:33` | reached only after an API write already refused/filtered; add a start-of-task frozen check as backstop |
| T7 | `tasks/rediarize_task.py:552` | reached via reprocess (refused); add start-of-task backstop |
| T8 | voice-match / speaker suggestion writes dispatched **after** completion (in `tasks/transcription/postprocess.py` chain) | skip frozen (initial run allowed, §3.1) |

**Count: 53 API write paths (W1-W53) + 8 task groups (T1-T8, 14 task functions) = 61
inventoried write paths**; 45 gain a new guard or filter, 6 keep an existing guard, 2 are allowed
with a new audit event, 8 task groups gain a skip.

### 4.5 Classified as not touching a held file's content (exempt, with reason, in the test table)

Uploads and file creation (`files/__init__.py:162`, `prepare_upload.py:316`, `complete_upload.py:189`,
`multipart.py:81`, `url_processing.py:193`): no existing file. Destroy paths
(`cancel_upload.py:25`, `trash.py:86/139/173`, `management.py:368`, `erasure_receipts.py`,
`self_erasure.py`, `privacy.py` purges, `watch_sources_files.py` deletes): already hold-guarded
as destroy paths (`DESTROY_GUARDED` class in the test, pointing at their guard). Derived copies
(`waveform.py:313/370`, `files/__init__.py:315` cache, `:392` analytics, `media_delivery.py:224`,
`transcript_document_export.py:86`, `subtitles.py:317`, search reindex / reembed routes):
regenerate copies of the frozen record. `management.py:164` cancel: only meaningful during the
initial run. Collection rename/visibility/shares, tag rename/shares/cleanup (unused tags have no
file links): labels and access, not content. Everything under settings, auth, admin config, chat,
prompts, vocabulary, SCIM, support access, org admin: no file content. The implementer fills the
exact list from the scanner's first run; every entry carries a one-line reason.

---

## 5. Auto "Legal hold" tag and collection: computed, not rows (choice and why)

**Choice:** a **virtual** system tag and collection, computed from `hold_guard.held_pred()` at read
time. No `tag`, `file_tag`, `collection` or `collection_member` rows are created.

Why real rows are worse here:
1. **Second source of truth.** `hold_guard` is "the ONE answer"; rows would be a cached copy that
   must be written on place, on release, on every collection-hold membership insert (the
   `after_insert` listener), on file delete, and repaired after any failure. A drifted row either
   hides a held file or labels a free one, the exact class of bug `test_hold_guard_single_source`
   exists to prevent.
2. **Existing system tags are global.** A system tag is visible in every tenant and a `file_tag`
   link has no tenant or user dimension. Real rows would leak "this file is held" to any surface
   that lists a file's tags, including the search index documents, exports and the AI
   auto-labeller, and would have to be special-cased in tag merge, delete, cleanup, promotion and
   tenant erasure.
3. **Collections have no system kind** (CHECK requires an owner or an organization). A per-tenant
   system collection needs a new kind, lazy creation per user/org, and exemptions in shares,
   visibility, default summary prompts, collection holds (a collection hold on the "Legal hold"
   collection would be circular) and the collection-delete guard.
4. **Not editable / not deletable** comes for free: there is nothing to edit or delete.
5. **Removed when the last hold releases** comes for free: the predicate stops matching.

Design:
- `GET /api/files` gains `held: bool | None = Query(None)`; `apply_hold_filter(query, held)` in
  `files/filtering.py` applies `held_pred()` / `not_held_pred()`; wired into `apply_all_filters`
  as a cheap filter. Scope is whatever the listing already scoped (ownership, tenant, quarantine
  exclusion), so a user only ever sees their own accessible held files: no cross-tenant leak and
  no reason anywhere.
- `get_metadata_filters` returns an additive `held_count: int` over the same scoped query (so the
  UI shows the chip and the sidebar entry only when there is something to show).
- The gallery list schema (`schemas/media.py` `MediaFile`) gains `hold: HoldSummary` (moved up
  from `MediaFileDetail`, which keeps inheriting it). The listing fills it with **one** batched
  `hold_states(page_ids)` call, never per row, and never with uuids on the list
  (`hold_uuids=[]`). This puts the badge on gallery cards.
- `GET /api/collections/{uuid}/media` gets the same `hold` field (same helper).
- UI (Lane D): the filter panel's tag list shows a locked pseudo-tag chip **"Legal hold"**
  (shield icon, not removable from a file, no edit/delete menu) that toggles `held=true`; the
  collections sidebar shows a locked pseudo-collection **"Legal hold"** with `held_count`,
  which opens the gallery filtered by `held=true`. Both appear only when `held_count > 0`.
- Search page: out of scope for v0.7.0 (the gallery filter answers "find my held files"); note it
  in the docs as gallery-only.

---

## 6. The structural test (fails when a new write path skips the guard)

`backend/tests/unit/test_hold_freeze_coverage.py`, modelled on
`tests/unit/test_ws_event_quarantine_discipline.py` (two AST scanners in one module, keyed
`"<path under app/>::<function>"`, never by line; mandatory written reason; stale entries fail;
self-test cases).

- **Scanner 1, routes.** Every function under `app/api/endpoints/**` decorated with
  `<name ending in "router">.{post,put,patch,delete}` (the AST walk used to build §4 found 339).
  Each key must be in exactly one table:
  - `FREEZE_GUARDED[key] = "<module>::<function>"`: the function (in the route's own module or in
    the named helper) must contain a call to one of `GUARD_CALLS = {"refuse_if_frozen",
    "assert_content_mutable", "refuse_reprocess_if_held", "hold_state", "hold_states",
    "assert_not_held", "held_pred", "not_held_pred"}`. The scanner verifies the call is
    present in that function body; a table entry whose function no longer calls a guard fails.
  - `DESTROY_GUARDED[key] = (helper, reason)`: same verification, for destroy paths.
  - `ALLOWED_ON_HELD[key] = reason`: W52/W53 (must also call `stage_content_event` with
    `CONTENT_HOLD_ANNOTATION`, verified).
  - `NOT_FILE_CONTENT[key] = reason`.
  A route in no table fails with "classify this write path for legal-hold freeze (see
  docs/design/legal_hold_ux_freeze_plan.md §4)".
- **Scanner 2, Celery tasks.** Every function decorated with `*.task(` under `app/tasks/**` and
  `app/services/**`: `FREEZE_GUARDED` / `DERIVED_COPY` / `NOT_FILE_CONTENT` with reasons, same
  verification (T1-T8 must call a guard).
- **Self-test:** a synthetic unguarded route must be reported; a synthetic guarded route must not;
  a stale entry must be reported. Without it an empty result is indistinguishable from a clean
  tree (repo rule, root `CLAUDE.md` "Four tools").
- Add the new file's categories to nothing in `audit-allowlist.txt`: it must audit clean.

Red-first: commit the test with the tables filled from the **current** guard state; it must fail
on W1-W45 before any guard lands (each listed as "classified FREEZE_GUARDED but calls no guard"),
then go green lane by lane inside Lane A.

---

## 7. Lane A: freeze enforcement (backend) — `v070/legal-hold-freeze`

Independently mergeable; no migration; no UI change required (the SPA already maps
`FILE_UNDER_LEGAL_HOLD` via `HOLD_REFUSAL_KEYS`).

Files:
- `backend/app/services/hold_guard.py`: add `FREEZE_REFUSAL`, `FREEZE_ACTIONS`,
  `is_frozen_state(state, status)` / `frozen_states(db, file_ids)` (held AND status not in
  pending/processing, one query), `assert_content_mutable(db, file_ids)` (raises
  `HoldRefusedError`; for services and tasks); rewrite `REPROCESS_REFUSAL`,
  `DESTROY_DERIVED_REFUSAL`; docstring note pointing at #46.
- New `backend/app/api/hold_refusal.py`: `refuse_if_frozen(db, file_ids, *, actor, action,
  message=FREEZE_REFUSAL, reveal_uuids=False)` → `record_hold_refusal` + 409. **Move**
  `refuse_reprocess_if_held` (`files/reprocess.py:129-165`) here as a thin wrapper over
  `refuse_if_frozen`, update its importers (`speakers.py:19`, `summarization.py:41`,
  `files/management.py:245`, and its own caller `files/reprocess.py:280`), delete the original
  (no two paths).
- Global FastAPI exception handler for an uncaught `HoldRefusedError` → 409
  `refusal_detail(merged, reveal_uuids=False, message=FREEZE_REFUSAL)` (register where other
  handlers live in `app/main.py`; check `backend/app/api/CLAUDE.md` for the convention).
- Guards at W1-W45 (§4.1), service-level guard in `services/tag_files.py` (`add_file_tag`,
  `remove_file_tag`) so W19/W20/W32 share one check; membership insert sites
  (`media_collections_membership.py:165`, `prepare_upload.py:167` is a new file so exempt,
  `auto_label_service.py:526`).
- `services/content_hold_service.py`: delete `record_edit_on_held_file`; add
  `record_hold_annotation(db, request, actor, comment, state)` (stages `content.hold.annotation`).
- `app/auth/audit_events.py`: add `CONTENT_HOLD_ANNOTATION`; docstring on
  `CONTENT_TRANSCRIPT_EDIT` (historic).
- `comments.py`: refuse edit/delete; annotate create.
- Tasks T1-T8.
- `backend/app/services/CLAUDE.md` **Holds** bullet: replace the `record_edit_on_held_file`
  sentence with the freeze rule and point at the structural test.
- `CHANGELOG.md` `### Changed` (transcript and metadata edits on held files are now refused) and
  `### Added` (`content.hold.annotation`).

Tests (watch each fail first, using a `git archive HEAD` red tree, root `CLAUDE.md`):
- `tests/unit/test_hold_freeze_coverage.py` (§6).
- `tests/api/test_hold_freeze_routes.py`: one parametrized case per W1-W45 on a held file → 409,
  `error == FILE_UNDER_LEGAL_HOLD`, row unchanged in DB, one `content.hold.refused` with the
  pinned `action`; the same request on an unheld file succeeds (control).
- Update, do not relax: `tests/api/test_speaker_merge_held_file_audit.py`,
  `tests/unit/test_held_file_edit_and_tag_share_audit.py`, `tests/unit/test_audit_outbox.py`
  cases that pin the edit event now pin the refusal (behaviour change, assertion as strict).
- `tests/api/test_hold_annotation.py`: comment create on held file commits with exactly one
  staged `content.hold.annotation`; rollback of the comment rolls back the event.
- `tests/unit/test_hold_freeze_tasks.py`: T1-T8 skip a frozen file and write nothing; a file held
  while `processing` completes its run (§3.1).
- `tests/api/test_redaction_lazy_scan_on_held.py` (§3.4 both halves).
- The §3.6 comment-erasure edge test.

Verification (lane-scoped, no full suite):
```bash
cd /mnt/nvm/repos/transcribe-app-wt-<lane>
scripts/test-in-worktree.sh -- -v -n 1 tests/unit/test_hold_freeze_coverage.py \
  tests/api/test_hold_freeze_routes.py tests/api/test_hold_annotation.py \
  tests/unit/test_hold_freeze_tasks.py tests/api/test_redaction_lazy_scan_on_held.py \
  tests/api/test_speaker_merge_held_file_audit.py tests/unit/test_held_file_edit_and_tag_share_audit.py \
  tests/unit/test_hold_guard_single_source.py tests/api/test_content_hold_guards.py
cd backend && python3 ../scripts/audit-tests.py tests   # 0 open findings
backend/venv/bin/ruff check <touched files> && backend/venv/bin/mypy <touched files>
```

---

## 8. Lane B: community operator hold (no quarantine) — `v070/legal-hold-operator`

### 8.1 Data model (`v467_add_content_hold_authority`)

Migration number: head is `v464`; `v465`/`v466` are reserved by the data-safety lanes, so this
lane takes **`v467`** and adds its row to the lane table in `backend/alembic/CLAUDE.md`. Re-derive
the head immediately before writing the file and chain after whatever is there (the coordinator
re-chains at merge). Follow `backend/alembic/CLAUDE.md` (idempotent guards, detection marker,
schema-detect registration under `app/db/schema_detect/`).

- `content_hold.authority VARCHAR(20) NOT NULL DEFAULT 'organization'`,
  `CHECK (authority IN ('organization', 'platform'))` (`ck_content_hold_authority`).
- `content_hold.organization_id` → **nullable**; `CHECK ((authority = 'organization') =
  (organization_id IS NOT NULL))` (`ck_content_hold_authority_org`). Existing rows are all
  `organization` with an org id, so both checks hold on upgrade.
- Partial index `ix_content_hold_platform_active ON content_hold (id) WHERE authority =
  'platform' AND released_at IS NULL`.
- Downgrade: refuse if any `platform` row exists (message names the count), else drop.
- Model constants `AUTHORITY_ORGANIZATION`, `AUTHORITY_PLATFORM` in `models/content_hold.py`.

Why a row and not the existing `media_file.legal_hold` flag: the flag has no reason, no matter
reference, no history, no collection scope and no release record, and its release is entangled
with the takedown (`release_file(clear_legal_hold=...)`). A `content_hold` row gets all of that,
the collection-membership listener, the storage object lock and the erasure nudge for free, and
`_active_hold_covers_file` already treats it as held everywhere. The flag stays as the
takedown's hold (public behaviour, unchanged).

### 8.2 `hold_guard` and queries

- `HoldState` gains `platform_hold_uuids: tuple[str, ...] = ()`; `platform` keeps meaning "the
  takedown flag" internally, and `held_by` reports `"platform"` when the flag **or** a platform
  hold covers the file (wire vocabulary unchanged: still `platform` / `organization`).
  Rename the internal field to `platform_flag` and update the single-source test allowlist
  accordingly; `held` = flag or any uuid.
- `hold_states` splits active hold uuids by `ContentHold.authority`.
- `refusal_detail(state, *, reveal_uuids, reveal_platform_uuids=False, message)`: org uuids to
  org admins (existing), platform uuids to platform admins only.
- `content_hold_queries.collection_hold_summaries`: derive `held_by` from each hold's authority
  (today hard-coded organization, `:103-110`).
- `list_holds(db, org_id)` unchanged (it filters by org id, so platform holds never appear on
  the org screen); new `list_platform_holds(db, *, active_only, limit, offset)`.
- `file_hold_summary(..., reveal_uuids, reveal_platform_uuids)`; `detail_response.py:546`
  passes `reveal_platform_uuids=current_user.role in ("admin", "super_admin")`.

### 8.3 Service

`content_hold_service.place_file_hold` / `place_collection_hold` / `release_hold` take
`authority` and `org_id: int | None`; platform holds skip the "belongs to org" backstops (a
platform admin may hold any file); everything else (reason bounds, 500-file cap, outbox audit,
object lock, erasure nudge) is shared. No second code path.

### 8.4 API (additive; nothing on master is renamed or removed)

New `backend/app/api/endpoints/admin_holds.py`, mounted at prefix `/admin/holds` next to
`admin_quarantine` (`api/router.py:211`), dependency `get_current_admin_user` (same gate as the
takedown routes), `refuse_under_grant=True`, **no capability** (an operator must always be able to
act on a legal demand, the same reasoning as the ungated `admin.takedown`,
`core/capabilities.py` docstring). Shapes reuse `schemas/content_hold.py`
(`HoldCreate`, `HoldOut`, `HoldList`, `HoldRelease`, `HeldFile`); `HoldOut` gains additive
`authority` and nullable `organization_uuid`.

| Route | Behaviour |
|---|---|
| `GET /api/admin/holds?active_only&limit&offset` | platform holds, newest first; `audit_metadata_access` like the quarantine list |
| `GET /api/admin/holds/{uuid}` | one platform hold with files (≤500) |
| `POST /api/admin/holds` | `scope=file` (`file_uuids`, any tenant, missing → 404) or `scope=collection` (any collection by uuid, missing → 404); rate limit `ADMIN_HOLD_RATE_LIMIT` (new constant beside `ORG_HOLD_RATE_LIMIT`); 201 |
| `POST /api/admin/holds/{uuid}/release` | release with reason; 409 if already released; files still held by another hold stay held |

Org-admin routes are unchanged except that their responses gain `authority: "organization"`.
Upgrade-page items: none of these are status-code changes for existing routes; Lane A's are
(§10).

Tests (red first): `tests/api/test_admin_holds.py` (non-admin 403/404, place file + collection,
reason bounds, release, reason never in file/collection/refusal/audit payloads, collection hold
covers later members); `tests/unit/test_platform_hold_destroy_paths.py` (a platform hold blocks
trash, purge, retention, account deletion, GDPR erasure; release nudges erasure; parametrized over
the existing destroy-path fixtures); migration test in the style of the existing `v4xx` tests
(upgrade with existing org holds, downgrade refusal); `test_hold_guard_single_source.py` updated
allowlist; `test_org_admin_holds.py` proves a platform hold never appears on the org list.

Verification:
```bash
scripts/test-in-worktree.sh -- -v -n 1 tests/api/test_admin_holds.py \
  tests/unit/test_platform_hold_destroy_paths.py tests/api/test_org_admin_holds.py \
  tests/unit/test_hold_guard_single_source.py <migration test>
python3 scripts/release-tests/lib/alembic-head.py backend   # prints v467_...
```

---

## 9. Lane C: virtual "Legal hold" tag/collection (backend) — `v070/legal-hold-virtual-filter`

Files: `files/filtering.py` (`apply_hold_filter`, wired in `apply_all_filters`;
`held_count` in `get_metadata_filters`), `files/listing.py` (`held` query param, batched `hold`
on items), the collection-media listing route, `schemas/media.py` (move `hold` to the list
schema). Additive only.

Tests: `tests/api/test_gallery_held_filter.py`: `held=true` returns exactly the held files the
caller can see, in each ownership mode; another tenant's held file never appears and never
counts; a quarantined held file stays hidden for a non-admin; release removes it from the filter
and the count with no other write; list items carry `hold.held` and never `hold_uuids`; query
count for a 100-row page is constant (one hold query).

Verification: `scripts/test-in-worktree.sh -- -v -n 1 tests/api/test_gallery_held_filter.py
tests/api/test_files_listing*.py`.

---

## 10. Lane D: UI — `v070/legal-hold-ui` (after A, B, C merge)

Frontend rules: Svelte + TS, `$lib`/`$components`/`$stores` aliases, light/dark parity, 2-space,
real browser check (`DISPLAY=:11`, root `CLAUDE.md` "Look at the UI").

Components:
- **`components/legal-hold/HoldExplainer.svelte`** (new): an info icon with tooltip
  (`holds.tooltip`) plus a `<details>` disclosure "What does a legal hold do?" listing what is
  kept, what is blocked, what still works, and who can release it. Used by the panel, the dialog
  and the banner. Logical CSS properties only (`margin-inline-start`, `inset-inline-end`) so `ar`
  renders mirrored; keyboard-operable; `aria-describedby` on the info icon.
- **`components/legal-hold/PlaceHoldDialog.svelte`** (new): reason (10-2000, live counter),
  optional matter reference, the target summary ("3 files" / a collection name), the explainer,
  submit. Calls `OrgAdminApi.placeHold` when the caller is an org admin with
  `org.legal_hold`, else `AdminApi.placeHold` (new `lib/api/adminHolds.ts`) when
  `role ∈ {admin, super_admin}`. One dialog, one decision function
  `holdAuthorityForCaller(capState, user)` in `lib/legalHold.ts` (unit-tested).
- **`LegalHoldsPanel.svelte`**: rename `OrgLegalHoldsPanel.svelte` and parameterise it with
  `scope: 'organization' | 'platform'` and an API adapter (one component, no copy). Org section
  `org-holds` unchanged; new platform section `platform-holds` in the Administration group of
  `SettingsModal.svelte` for admins (see `docs/design/861_settings_sidebar_regroup_plan.md` for
  placement), with a note row: "Holds placed with a takedown are managed in Abuse and takedown."
  Release stays here only. Add the explainer at the top.
- **Gallery** `GallerySelectionActions.svelte`: "Place legal hold" in the Organize menu, visible
  when `holdAuthorityForCaller(...) !== null`, enabled with ≥1 selected, max 500 (message
  otherwise); wired in `routes/+page.svelte` like `QuarantineModal` (`:757-771`); on success
  update `hold` on the affected rows locally and toast. The file-card / list row renders
  `HoldBadge` from the new list field.
- **Gallery filter / sidebar**: locked pseudo-tag chip "Legal hold" in `GalleryFilterPanel.svelte`
  and pseudo-collection "Legal hold" in `collections/CollectionsList.svelte`, shown only when
  `held_count > 0`, driving `held=true` through the gallery store (new
  `filterHeld: boolean` in `$stores/gallery`, persisted like the other filters). No edit, delete,
  share or rename affordance on either.
- **File detail** (`routes/files/[id]/+page.svelte`, `FileHeader.svelte`):
  - "Copy file ID" button next to the title (clipboard API, toast "File ID copied"); for
    everyone (the ID is not sensitive to someone who can open the file).
  - "Place legal hold" action in the header menu for admins (same dialog, one file).
  - **Held-file banner** when `file.hold.held`: title, one-line plain explanation, who holds it
    (by `held_by`, never the reason), the explainer disclosure.
  - Disable (not hide) every frozen control when held, with the tooltip: segment edit,
    speaker rename/merge/link/verify, title/metadata edit, summary generate/regenerate/delete,
    topics extract/apply/dismiss, tag and collection editing for that file, comment edit/delete,
    reprocess. New comment box stays enabled with a hint "Comments on a held file are recorded."
  - 409 handling: a stale page that still sends an edit shows the `HOLD_REFUSAL_KEYS` message.
- **Strings to correct** (all 12 locales): `holds.description`, `holds.badgeTitle.*`,
  `holds.refusal.*` say "deleted or reprocessed"; they become "changed, deleted or reprocessed".

i18n (all 12 locales, real translations, `ar` RTL checked visually; `npm run check:i18n` is
parity only): `holds.tooltip`, `holds.explainer.title`, `holds.explainer.intro`,
`holds.explainer.kept`, `holds.explainer.blocked`, `holds.explainer.allowed`,
`holds.explainer.release`, `holds.banner.title`, `holds.banner.byOrganization`,
`holds.banner.byPlatform`, `holds.banner.byBoth`, `holds.action.place`,
`holds.action.placeOnFile`, `holds.dialog.title`, `holds.dialog.targetFiles`,
`holds.dialog.targetCollection`, `holds.dialog.tooMany`, `holds.dialog.placed`,
`holds.frozenControl`, `holds.commentHint`, `holds.virtual.tag`, `holds.virtual.collection`,
`holds.platform.navLabel`, `holds.platform.title`, `holds.platform.description`,
`holds.platform.takedownNote`, `fileDetail.copyId`, `fileDetail.copyIdDone`, plus the audit
label for `content.hold.annotation`.

Tests (vitest, red first): `HoldExplainer.test.ts` (renders, disclosure toggles, no reason
prop exists); `PlaceHoldDialog.test.ts` (reason bounds, picks the org vs platform API by
caller, 500 cap); `legalHold.test.ts` (`holdAuthorityForCaller` truth table: org admin with cap,
platform admin, both, regular user → null); `GallerySelectionActions.test.ts` (action hidden for
regular users, present for admins); `LegalHoldsPanel.test.ts` (both scopes; rename of the old
test file); file detail: frozen controls disabled when `hold.held`, comment box enabled; filter
chip shown only when `held_count > 0`. Run `npm run test:audit`.
E2E (pytest + Playwright, `backend/tests/e2e/`, cleans up after itself: place then release with
reasons): admin places a hold from the gallery, the badge appears, a regular user's edit control
is disabled and a forced edit returns the refusal message, the "Legal hold" chip lists the file,
release from Settings removes chip and badge.

Verification:
```bash
cd frontend && npm run test -- legal-hold HoldBadge LegalHoldsPanel GallerySelectionActions \
  && npm run check && npm run check:i18n && npm run test:audit
DISPLAY=:11 pytest backend/tests/e2e/test_legal_hold_ui.py -v --headed   # against the lane's own stack
```
Screenshots for docs via the `docs-screenshots` skill after merge.

---

## 11. Lane E: documentation — `v070/legal-hold-docs` (merge after A and B)

Open-source docs-site (use real UI labels from `en.json`; build must pass, links are `throw`):
- **New** `docs-site/docs/user-guide/legal-holds.md`, added to `sidebars.ts` under User Guide:
  1. For everyone: what the badge and banner mean; what you can still do (read, search, play,
     export, download, comment) and what you cannot (the §3.1 table in plain words); why
     (preserving information as it was, short §1 summary with links to the FRCP rule text and
     The Sedona Conference); who to ask (an administrator; you will not see the reason).
  2. Hold vs quarantine vs trash vs delete: one table (who sets it, can the owner see it, does
     it block delete/retention/erasure/reprocess/edits, how it ends, is it reversible).
  3. Finding held files: the "Legal hold" filter chip and sidebar entry (gallery only).
  4. For administrators: placing from the gallery, from the file page, by pasting IDs (with
     "Copy file ID"), or by collection; releasing in Settings > Legal holds; the reason is
     admin-only; collection holds cover later files; what the audit log records
     (`content.hold.place/release/refused/annotation`); precedence (hold > trash purge >
     retention; erasure deferred under GDPR Art. 17(3)(e), retried on release).
  5. Self-hosted (community) operator holds: admins hold files or collections without a takedown
     (Settings > Legal holds), the API (`/api/admin/holds`), and how it differs from the
     takedown's hold.
- `admin-panel.md:415-452`: replace "Editing a transcript is still allowed" and the
  `content.transcript.edit` sentence; shorten to a summary linking the new page; add an
  "Operator legal holds" subsection.
- `docs/abuse-and-takedown.md:276-301`: add an "Operator hold (no takedown)" column; change the
  "Transcript edits" row to "Refused (a hold freezes the content)" for all hold kinds; add the
  `content_hold.authority` storage fact.
- `tags.md`, `collections.md`, `search-and-filters.md`: one line each on the locked "Legal hold"
  entries.
- `docs-site/docs/operations/upgrading-to-0-7-0.md`, "Behaviour changes": the W1-W45 routes now
  answer `409 FILE_UNDER_LEGAL_HOLD` on held files where they used to succeed (list the routes);
  `content.transcript.edit` is no longer emitted for held files, `content.hold.refused` is;
  new `content.hold.annotation`; additive fields (`hold` on list items, `held` filter,
  `held_count`, `authority`); new `/api/admin/holds` routes; integrators that edit held files
  programmatically must release the hold first.
- `CHANGELOG.md` entries for B, C, D (A carries its own).

Verification: `cd docs-site && npm ci && npm run build`.

### Managed edition docs (for the managed edition's own documentation; no code here)

Organization holds exist only where `org.legal_hold` is enabled, so these pages belong to the
managed edition's docs, not the open-source site:
- Organization administrator guide: placing holds from the gallery selection and the file page
  (the same dialog routes to `/api/org-admin/holds`), the org Legal holds panel with the
  explainer, release with reason, matter references.
- That a file may also be held by the **service operator** (badge "held by the service
  operator"), which an organization administrator cannot release, and whom to contact.
- That platform staff cannot place or release an organization's hold, and support-access
  sessions are refused on the hold screens.
- The freeze rules and the hold/quarantine/trash/delete table, reusing the open-source page's
  wording, plus the organization-erasure deferral.
- The capability key (`org.legal_hold`) for whoever configures tiers.

---

## 12. Sequencing (waves; one full sweep per wave)

| Wave | Lane | Branch | Depends on | Touches |
|---|---|---|---|---|
| 1 | A freeze enforcement | `v070/legal-hold-freeze` | none | `hold_guard.py` (additions), `api/hold_refusal.py`, ~25 endpoint/task files, comments, audit events, tests |
| 1 | B operator hold | `v070/legal-hold-operator` | none (re-chain v467 at merge) | migration, `content_hold.py`, `hold_guard.py` (`HoldState`, `hold_states`, `refusal_detail`), queries, service, `admin_holds.py`, router, schemas, tests |
| 1 | C virtual filter | `v070/legal-hold-virtual-filter` | none | `filtering.py`, `listing.py`, collection media route, `schemas/media.py`, tests |
| 2 | D UI | `v070/legal-hold-ui` | A, B, C merged | frontend only + one e2e |
| 2 | E docs | `v070/legal-hold-docs` | A, B merged (C/D for the chip and UI wording; finalize after D) | docs-site, `docs/abuse-and-takedown.md`, CHANGELOG |

A and B both edit `hold_guard.py` and `content_hold_service.py`: A only appends functions and
deletes `record_edit_on_held_file`; B changes `HoldState` and the place/release signatures.
Merge B first if both finish together (A's helpers build on `hold_states`, which B changes but
keeps the signature of). Each lane: own worktree, own commits with hooks on, targeted tests only;
the coordinator merges `--no-ff` and runs the wave sweep.

Whole-tree guard list for each wave sweep (coordinator, on the merged tree, quiet checkout):
```bash
scripts/safe-precommit.sh run --all-files
scripts/safe-precommit.sh run --all-files --hook-stage pre-push
python3 scripts/audit-tests.py backend/tests
cd frontend && npm run test:audit && npm run test:audit:selftest && npm run check:i18n \
  && npm run check && npm run build && npm run test
cd docs-site && npm run build
./scripts/run-integration-tests.sh          # unit/API + FIPS re-run + integration + schema drift
./scripts/e2e/run-e2e.sh -m settings        # wave 2 (plus the new legal-hold e2e)
python3 scripts/release-tests/lib/alembic-head.py backend   # wave 1: v467 head
```
Named whole-tree tests that must stay green: `test_hold_guard_single_source.py`,
`test_hold_freeze_coverage.py`, `test_precommit_stage_ci_parity.py`,
`test_precommit_mypy_env_parity.py`, the schema-drift check in `run-integration-tests.sh`.

---

## 13. Open questions (owner-only), each with the default the lanes implement

1. **Which admin roles may place an operator hold in the community edition: `admin` and
   `super_admin`, or `super_admin` only?** Default: both, the same gate as quarantine
   (`get_current_admin_user`), because both can already set the takedown's hold today.
2. **Should the operator-hold routes also be available to the managed edition's platform staff?**
   Default: yes, ungated like the takedown queue (an operator must be able to act on a legal
   demand); the screen never shows organization holds' reasons, and organization admins see such
   a file as "held by the service operator".
