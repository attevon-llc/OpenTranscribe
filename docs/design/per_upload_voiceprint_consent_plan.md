# Per-upload voiceprint consent and per-file voiceprint opt-out: design and implementation plan

Status: PLAN ONLY (2026-11-01). Nothing here is implemented.
Companions: `docs/design/legal_records_and_voiceprint_retention_plan.md` (WP-E2 adds
`PurgeScope(kind="files")`, a hard prerequisite for the later-opt-out purge),
`docs/design/self_service_erasure_and_reconcile_plan.md` (takes the next migration number first if
it lands first), `docs/design/erasure_receipt_plan.md`.

## 0. Summary

- A **per-file consent state** set at upload time (checkbox "Speakers in this recording agreed to
  voice identification"), changeable later by the owner.
- A file whose state is `declined` (opt-out) never gets voiceprints: `voiceprint_decision` denies
  it and the OpenSearch write backstop refuses writes for it.
- Opting out after processing purges that file's voiceprints with `PurgeScope(kind="files")` and
  recomputes affected profile centroids (that machinery comes from the legal plan WP-E2).
- Restrict-only: the file layer can only turn voiceprints **off**. It never enables what the
  instance, platform ceiling, organization or user layer has turned off.
- Optional DB setting `privacy.voiceprint_upload_consent_required` (default **false**): when on, a
  file is treated as declined unless its state is `granted`.

Migration: head today is **v449**; v450/v451 are reserved by the legal plan; the self-service
erasure plan proposes the next. **Re-derive with `python3 scripts/release-tests/lib/alembic-head.py
backend` right before writing**; written below as `v453_add_media_file_voiceprint_consent.py`.

## 1. Current state (derived from code)

- `backend/app/services/privacy/policy.py`: `_voiceprint_decision(db, user_id, organization_id,
  media_file_id)` resolves layers (`_resolve`), then the consent hooks with a
  `VoiceprintConsentRequest(purpose="create", ..., media_file_id=...)`.
  `voiceprint_decision_for_file` reads `(user_id, organization_id)` from `media_file` and passes
  `media_file_id`. `assert_voiceprint_write_allowed(user_id, organization_id, *, db=None)` is per
  **subject**, cached per `(user_id, organization_id)` for `PRIVACY_WRITE_BACKSTOP_TTL_SECONDS`, and
  passes `media_file_id=None` (its docstring states a per-file hook cannot stop this backstop).
- `backend/app/services/privacy/gates.py` `voiceprints_allowed(file_id, db)`: the entry gate used
  by the pipeline (`tasks/transcription/*`, `speaker_embedding_task`, `rediarize_task`,
  `reindex_task`, `embedding_consistency_repair`, `speaker_matching_service`, ...).
- `backend/app/services/privacy/consent.py`: hook registry; `VoiceprintConsentRequest` frozen
  dataclass (seam surface).
- Backstop call sites: `opensearch_service/speaker_write.py` (lines ~60, ~167 per-entry in bulk,
  ~267), `profiles.py` (~52, ~249), `clusters.py` (~51, ~240).
- Uploads: `backend/app/api/endpoints/files/prepare_upload.py` (creates the `MediaFile` row;
  request carries `extracted_from_video`, `file_hash`), `complete_upload.py`
  (`CompleteUploadRequest`: `file_id`, `parts`, `disable_diarization`, ... and dispatches
  processing). Other entry points create files too: legacy `upload.py`, URL ingestion, recordings,
  watch sources (`services/watch_sources/`).
- `MediaFile` JSONB columns: `metadata_raw`, `metadata_important` (display metadata, written by
  extraction and by prepare for extracted video).
- SPA: upload wizard steps in `frontend/src/components/upload/` (`UploadStepSpeakers.svelte`,
  `UploadStepReview.svelte`, `MediaFilePanel.svelte`, ...); privacy UI in
  `frontend/src/components/settings/UserPrivacyCard.svelte`; `frontend/src/lib/settings/privacyPolicy.ts`.

## 2. Decisions (recommendations; owner to confirm in §12)

- **D1 Column, not JSONB.** Add `media_file.voiceprint_consent varchar(10) NOT NULL DEFAULT
  'unspecified' CHECK IN ('unspecified','granted','declined')` and `voiceprint_consent_at
  timestamptz NULL`, `voiceprint_consent_by_user_id int NULL REFERENCES "user"(id) ON DELETE SET
  NULL`. Reasons: (1) the gate reads it on every decision, a typed column is cheap and indexable;
  (2) a CHECK makes an invalid state unrepresentable, JSONB would accept any value; (3)
  `metadata_important` is extraction output that is rewritten by extraction and reprocessing, so a
  consent value there could be overwritten silently; (4) the ORM/DDL divergence test covers columns.
  Tri-state rather than a bool because "never asked" and "said yes" differ once the required
  setting is on.
- **D2 Effective rule (file layer)**: deny when `declined`, or when the required setting is on and
  state is not `granted`. Evaluated **after** the stored layers and **before** the consent hooks
  (cheap deny first; hooks still receive `media_file_id`). `locked_by="file"`.
- **D3 Write backstop becomes file-aware.** `assert_voiceprint_write_allowed(user_id,
  organization_id, *, db=None, media_file_id: int | None = None)`. When `media_file_id` is given:
  subject decision (cached as today) AND the file layer (a fresh read, never cached: opt-out must
  bite immediately; one indexed PK read per write). Additionally pass `media_file_id` into
  `_voiceprint_decision` so consent hooks finally get the file at the backstop (closes the gap the
  docstring documents). Profile and cluster centroid writes have no single file: they stay subject
  scoped, and the centroid computation must **exclude speakers from declined files** (D5).
- **D4 Where it is set.** `prepare` accepts `voiceprint_consent: Literal["granted","declined"] |
  None` and writes it with the row (before any processing exists). `complete` accepts the same
  field but may only **restrict** (`granted`/`unspecified` → `declined`); an attempt to upgrade at
  complete is ignored with a logged warning, never a 4xx (clients retry complete). Other entry
  points default to `unspecified`; watch sources gain an optional per-source default (later; listed
  as follow-up, not in this plan's scope beyond the default).
- **D5 Later changes.** `PATCH /api/files/{uuid}/voiceprint-consent {state}` by the file owner
  only (org admins restrict via org policy, not per file). `→ declined`: set the column, clear the
  backstop cache, dispatch the two-pass purge with `PurgeScope(kind="files", file_ids=(id,))`,
  `reason="opt_out"` (extend the legal plan's `reason` parameter, which proposes `"retention"`; both
  bypass the "policy still enabled" check). `declined → granted`: allowed; no automatic
  re-extraction; the response says "reprocess speakers to create voiceprints" and the UI offers the
  existing re-diarize action.
- **D6 Org policy interplay.** Restrict-only both ways: org off ⇒ file `granted` changes nothing;
  org on ⇒ file `declined` still denies. An org may set the required setting at org level later via
  `org.defaults`; out of scope here, noted in §11.
- **D7 Audit.** `privacy.file_consent.change` with file id, old/new state, actor id; no file name.

## 3. Migration `backend/alembic/versions/v453_add_media_file_voiceprint_consent.py`

Per `backend/alembic/CLAUDE.md`:

1. `ALTER TABLE media_file ADD COLUMN IF NOT EXISTS voiceprint_consent varchar(10) NOT NULL DEFAULT 'unspecified';`
   (Postgres 11+ adds a constant default without a table rewrite; confirm the server version in
   compose.)
2. `ADD COLUMN IF NOT EXISTS voiceprint_consent_at timestamptz NULL;`
3. `ADD COLUMN IF NOT EXISTS voiceprint_consent_by_user_id integer NULL;` + FK
   `fk_media_file_voiceprint_consent_by_user` `ON DELETE SET NULL`. Add it to
   `test_user_deletion_fk_coverage.py`'s expectations (it derives FKs to `user`).
4. `ck_media_file_voiceprint_consent` CHECK; copy the normalized form back from `pg_get_constraintdef`.
5. Partial index `ix_media_file_voiceprint_declined ON media_file (id) WHERE voiceprint_consent =
   'declined'` (cheap listing for repair jobs). Detection arm keyed on it.

Downgrade: drop index, CHECK, FK, columns. Data loss on downgrade is the consent state itself; the
downgrade must refuse when any row is `declined` unless `OT_ALLOW_CONSENT_DOWNGRADE=1` is set
(otherwise an older build would start creating voiceprints for opted-out files).

Model: `backend/app/models/media.py` `MediaFile` gains the three columns; constant
`VOICEPRINT_CONSENT_STATES` beside it is the single source for CHECK and schema `Literal`.

## 4. Backend changes

1. `services/privacy/policy.py`:
   - `_file_consent_allows(db, media_file_id) -> bool` (via `_isolated`; read error denies).
   - `_voiceprint_decision`: after the layers, if `media_file_id` is not None and not
     `_file_consent_allows` → `Decision(False, "file")`.
   - `_file_scope` also returns `voiceprint_consent` to avoid a second read in
     `voiceprint_decision_for_file`.
   - `assert_voiceprint_write_allowed(..., media_file_id=None)` per D3. Cache key unchanged;
     the file check is outside the cache.
   - Add `"file"` to the `locked_by` vocabulary wherever it is enumerated (schemas, SPA
     `privacyPolicy.ts`).
2. Backstop call sites: pass `media_file_id` where the document has one
   (`speaker_write.add_speaker_embedding_v4`, `add_speaker_embedding`, and per entry in
   `bulk_add_speaker_embeddings_v4` from `data.get("media_file_id")`; verify the field name in the
   v4 mapping). `profiles.py`/`clusters.py`: no file id; unchanged.
3. Centroids: `services/profile_embedding_service.py` (and cluster centroid computation) must
   exclude speakers whose `media_file.voiceprint_consent = 'declined'`. Find every aggregation with
   `rg -n 'embedding' backend/app/services/profile_embedding_service.py backend/app/services/opensearch_service/clusters.py`.
   Without this, a declined file's voice survives inside a profile average.
4. Uploads: `prepare_upload.py` request model gains `voiceprint_consent`; set the three columns
   when the field is present (`by_user_id` = current user). `complete_upload.py` per D4.
5. New endpoint in `backend/app/api/endpoints/files/voiceprint_consent.py` (one concern), schemas
   in `backend/app/schemas/media.py` or a new `schemas/file_consent.py`. Owner-only; 404 for files
   the caller cannot see; refuse support grants; rate limited. Response includes
   `purge_task_id` when a purge was dispatched; status via existing `GET /privacy/purge/{task_id}`.
6. `GET /privacy/effective?file_uuid=` already exists: it must now report `locked_by="file"`.
7. File responses (`MediaFile` detail schema) expose `voiceprint_consent` (owner and org admins).
8. Setting `privacy.voiceprint_upload_consent_required` in `core/constants.py` defaults + the admin
   privacy page (`GET|PUT /admin/privacy-policy`), audited.
9. Consent hooks: unchanged signature. Document that `media_file_id` is now non-None at the
   backstop for speaker documents, so an extension hook can rely on it there.

## 5. SPA

- Upload wizard: a checkbox in `UploadStepSpeakers.svelte` (diarization/speaker step), default
  **unchecked**, label "Speakers agreed to voice identification for this recording"; help text
  explains what a voiceprint is. Unchecked sends `unspecified` (setting off) or is shown as required
  (setting on: the step shows that voiceprints will not be created, not a blocking error).
  Shown in `UploadStepReview.svelte`. Hidden entirely when the effective policy (from
  `privacyPolicy.ts`) already has voiceprints off.
- Bulk uploads: the one checkbox applies to every file in the batch (state in the upload store;
  `$stores` alias).
- Record and URL panels (`MediaRecordPanel.svelte`, `MediaUrlPanel.svelte`): same control if they
  go through prepare; otherwise follow-up.
- File detail: a "Voice identification" toggle in the file's speaker/privacy section with a
  confirmation dialog on opt-out ("existing voiceprints from this file will be deleted"), and a
  purge progress indicator using the existing purge status polling.
- i18n: about 14 keys (`upload.voiceprintConsent.*`, `file.voiceprintConsent.*`,
  `privacy.lockedBy.file`) in all 12 locales (`ar de en es fr it ja ko nl pt ru zh`), real
  translations; verify `ar` RTL layout of the checkbox row and dialog. Run `npm run check:i18n`.
- Light/dark parity; open the page in a browser before reporting done.

## 6. Sequencing

1. Prerequisite: legal plan WP-E2 (`PurgeScope(kind="files")`, centroid recompute) merged.
2. v453 migration + model.
3. Policy + backstop + call sites + centroid exclusion.
4. Prepare/complete fields; consent endpoint; setting.
5. SPA + i18n.
6. Docs, CHANGELOG, seam.

## 7. Tests that must fail first

Watch each red in a `git archive HEAD` tree (`mktemp -d -t ot-redcheck.XXXXXX`).

| # | File::test | Asserts |
|---|---|---|
| C1 | `backend/tests/unit/test_privacy_file_consent.py::test_declined_file_denies_with_locked_by_file` | |
| C2 | `::test_granted_file_does_not_override_org_off` (restrict-only) | |
| C3 | `::test_required_setting_denies_unspecified` | |
| C4 | `::test_file_layer_runs_before_hooks_and_hooks_get_file_id` | hook spy sees `media_file_id` |
| C5 | `backend/tests/unit/test_write_backstop_file_aware.py::test_backstop_refuses_declined_file_even_when_subject_cached_allowed` | cache warm then opt-out → refused immediately |
| C6 | `::test_bulk_write_skips_only_declined_entries` | |
| C7 | `backend/tests/api/test_upload_voiceprint_consent.py` | prepare stores state; complete may downgrade, not upgrade; invalid value 422 |
| C8 | `backend/tests/api/test_file_voiceprint_consent_endpoint.py` | owner only; non-owner 404; support grant refused; opt-out dispatches purge with files scope |
| C9 | `backend/tests/integration/test_file_opt_out_purge.py` (live OpenSearch + Postgres, isolated `--fresh` stack) | file A declined after processing: A's speaker docs gone, residual 0, profile centroid equals B-only vector, B untouched |
| C10 | `backend/tests/unit/test_profile_centroid_excludes_declined.py` | |
| C11 | migration test: upgrade twice idempotent; default `unspecified`; downgrade refuses with declined rows | |
| C12 | vitest `UploadStepSpeakers.voiceprintConsent.test.ts`; Playwright `backend/tests/e2e/test_upload_voiceprint_consent.py` (uploads a test file, deletes it after) | |

`python3 scripts/audit-tests.py backend/tests` and `npm run test:audit` to 0 findings.

## 8. Upgrade and rollback

- Upgrade: every existing file becomes `unspecified`, so behaviour is unchanged unless the operator
  turns the required setting on. **Turning it on later denies voiceprints for all existing files**
  (they are `unspecified`) but does not purge them; say so in the setting's help text and docs, and
  offer no automatic purge (that would be a bulk destruction triggered by a settings toggle).
- Rollback: code rollback leaves the columns ignored, so declined files would get voiceprints on
  reprocessing. Runbook: before rolling back, list declined files
  (`ix_media_file_voiceprint_declined`) and turn voiceprints off for those owners, or do not roll
  back. Migration downgrade refuses per §3.

## 9. Docs-site, CHANGELOG, seam

- docs-site: update `docs-site/docs/features/privacy-controls.md` (file layer, restrict-only
  table gains a row, required setting); user guide page for uploading (find with
  `rg --files docs-site/docs/user-guide | rg -i upload`) gains the checkbox; screenshots via the
  `docs-screenshots` skill after the UI lands.
- `CHANGELOG.md` `## [Unreleased]`: Added (per-upload voiceprint consent, per-file opt-out with
  purge, required-consent setting); Changed (write backstop checks the file when known).
- Seam (`backend/app/auth/constants.py` `CLOUD_SEAM_VERSION`): `assert_voiceprint_write_allowed`
  gains a keyword-only `media_file_id`; consent hooks now receive a file id at the backstop;
  `locked_by` gains `"file"`; `MediaFile` gains columns. Additive; share the bump with the other
  v0.7.0 plans if released together. Update `backend/app/services/search/CLAUDE.md` (backstop
  paragraph) and `backend/app/services/CLAUDE.md` privacy section.

## 10. Risks

1. **Centroid leakage**: if any aggregation path is missed, an opted-out voice survives inside a
   profile or cluster average. Mitigation: C9/C10 plus an `rg` inventory recorded in the PR.
2. **Checkbox semantics**: the uploader asserts consent on behalf of other people. The product
   records the assertion and who made it; it does not verify it. Wording must not overclaim.
3. **Ingestion paths that bypass prepare** default to `unspecified`; with the required setting on,
   watch-source imports silently get no voiceprints. Documented; per-source default is follow-up.
4. **Per-write PK read** in the backstop adds load on bulk embedding writes; batch the read per
   bulk call (one `IN` query) rather than per entry.
5. **Dependency on legal plan WP-E2**; without it, opt-out can only deny future writes, not purge.

## 11. Out of scope / follow-ups

Org-level required-consent default; watch-source per-source consent default; consent evidence
export per file.

## 12. Decisions for the product owner

1. Column (recommended) versus JSONB for the stored state.
2. Checkbox default unchecked, and the required setting default off: acceptable?
3. Should only the file owner change consent later (recommended), or also org admins?
4. Opt-out purge is immediate and irreversible for that file's voiceprints: confirm, given that
   re-granting requires reprocessing.
