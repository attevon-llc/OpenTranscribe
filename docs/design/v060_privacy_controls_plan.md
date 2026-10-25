# v0.6.0 privacy controls: one policy layer for voiceprints, inferred gender and the org redaction floor (#1280, #1279, with #1132). Design

Status: **PLAN ONLY.** Nothing here is implemented. Written 2026-10-25 against `master` @ `a236125a`.
Line numbers drift, so search for the quoted symbol rather than trusting a number.

Issues: [#1280](https://github.com/attevon-llc/OpenTranscribe/issues/1280) (v0.6.0),
[#1279](https://github.com/attevon-llc/OpenTranscribe/issues/1279) (v0.6.0),
[#1132](https://github.com/attevon-llc/OpenTranscribe/issues/1132) (v0.9.0, `epic:compliance`).
Related: #1200 (closed), #982/#987/#988 (redaction floor seam), #1122 (platform-admin bypass), #1253
(org takedown, the org-admin surface pattern this plan copies), #796 (account export).

## 0. Summary

One **privacy policy layer** answers two questions for a (owner, organization) pair: *may voiceprints
exist / be matched?* and *may gender be inferred?* Four restrict-only layers are ANDed: platform
(extension resolver) → instance (`SystemSettings`) → organization (new `organization_policy` row) →
user (`UserSetting`). Any layer off = off. The same org row also stores the **org redaction floor**,
which core unions with whatever a registered floor resolver returns. Enforcement is in three tiers:
the pipeline plan (no voiceprint stage is scheduled), an entry gate on every producer and consumer, and
a write backstop in the OpenSearch speaker-write functions. Turning a layer off **hides** existing data
and never deletes it; an explicit, audited **purge** action deletes it. #1280 builds the layer, the
consent seam and enforcement. #1132 is re-scoped to build the consent record, profile lifecycle,
export and access log on top of it, with no second gate.

## 1. Verified current state

### 1.1 Organization settings and the redaction floor

- `Organization` (`backend/app/models/organization.py:33-56`) has no settings or policy columns. There
  is no org-settings table, API or UI anywhere in core.
- The org redaction floor exists only as a resolver seam: `core/tenant_limits.py` `RedactionFloor`
  (`:79-120`), `resolve_redaction_floor` (`:327-350`), community resolver returns `None`. It is unioned
  in by `services/redaction/config.py` `_apply_tenant_floor` (`:201`) inside `resolve_effective_config`
  (`:239-260`). Callers now thread the org: `api/endpoints/search.py:734`,
  `tasks/transcription/postprocess.py:736`, `files/crud.py:592`, `files/subtitles.py:68`, chat,
  summarization and more (`rg -n "resolve_effective_config\(" backend/app`).
- The operator floor is `SystemSettings` `redaction.force_*`, edited at `POST /admin/redaction-policy/update`
  (`api/endpoints/redaction_settings.py:245-270`, super_admin) and rendered by
  `frontend/src/components/settings/RedactionPolicySettings.svelte`.
- Org-admin surfaces: `require_org_admin` (`api/deps_context.py:234-262`), `/org-admin` routers mounted
  with a capability in `api/router.py:179-200` (`org.takedown` uses `platform_admin_bypass=False`).
  Frontend org group: `SettingsModal.svelte` `orgAdminCapOn` (`:338-339`) and the nav block (`:405-420`).
- Capability `org.defaults` exists (`core/capabilities.py:136`, audience `org_admin`, community
  `False`) and is listed as tier 3, "declared but unread" (`:58-61`).

**Corrections to #1279.**
1. "Configurable through the API only" is wrong for core: there is no core API or storage. Only a
   derived deployment's registered resolver can supply a floor today. The screen therefore needs core
   storage and an API first (§3).
2. The stale claim "no core caller passes an org id" is **not in any docstring**. It lives in the
   commit message of `44ad77f9` (#982), which cannot be edited. The only misleading code text left is
   `services/redaction/config.py:251-253` ("every existing two-argument caller is unaffected"):
   production callers all pass the org now; the remaining two-argument callers are scripts
   (`app/scripts/verify_redaction.py:88`, `app/scripts/corpus_injection/injector.py:332`) and a
   docstring (`tasks/redaction_task.py:7`). `tenant_limits.py:124,157` ("community edition is ALWAYS
   None here") stays true. No CLAUDE.md repeats the claim (`rg -n -i "floor" --glob '*CLAUDE.md'`).

### 1.2 Voiceprint producers and consumers (what "disabled" must cover)

A **voiceprint** here is any persisted per-speaker or per-profile embedding (OpenSearch `speakers` alias,
`speakers_v3`, `speakers_v4`, profile docs) and the matching artifacts derived from it. Within-file
diarization (labelling SPEAKER_00..N) is not a voiceprint and keeps running: the diarizer clusters with
transient in-memory embeddings that are never returned when disabled.

| # | Site | Role |
|---|---|---|
| P1 | `services/pipeline_plan.py` `resolve_pipeline_plan`, `SpeakerIdStep` (`:189-195`, `:390-411`) | decides `speaker_id.execution` (`inline_native` / `embedding_stage` / `none`) |
| P2 | `transcription/diarizer_native.py:1030-1041` (`enable_native_embeddings`), `transcription/diarizer.py:353-357`, per-job `transcription/engine/config.py:205,244` | diar-native / PyAnnote return centroids |
| P3 | `tasks/transcription/finalize.py:259-268` serializes `native_embeddings` into the chain | |
| P4 | `tasks/transcription/postprocess.py:204-242` branch; `_process_native_embeddings` (`:618`), `_store_v4_centroids` (`:661`), `_dispatch_embedding_stage` (`:409`) | inline match + store |
| P5 | `tasks/transcription/embeddings.py` `_process_speaker_embeddings_native` (`:66`), `_update_v4_profile_embeddings` (`:223`), `_store_native_centroids_in_v4_staging` (`:283`) | |
| P6 | `tasks/speaker_embedding_task.py` `extract_speaker_embeddings_task` (`:300`), `update_speaker_embedding_on_reassignment` (`:393`; dispatched from `api/endpoints/transcript_segments.py:213`) | CPU voiceprint stage |
| P7 | `tasks/rediarize_task.py` `_voiceprint_stage` (`:354`), does **not** consult the plan's `speaker_id` | |
| P8 | `services/speaker_matching_service.py` `process_speaker_embeddings_native` (`:600`), `match_speaker_to_profiles` (`:174`), `find_and_store_speaker_matches` (`:1581`), writes at `:485,841,912,994,1335` | match + auto-suggest |
| P9 | `services/profile_embedding_service.py` `update_profile_embedding` (`:327`), batch (`:502`; `api/endpoints/admin.py:2911`); `api/endpoints/speaker_update.py` `_update_profile_embedding` (`:234`); `services/speaker_cleanup.py:352` | profile averages |
| P10 | merge: `api/endpoints/speakers.py` `_merge_speaker_embeddings` (`:2526`, write `:2556`), `tasks/speaker_merge_task.py:122`, `opensearch_service/speaker_maintenance.merge_speaker_embeddings` | |
| P11 | clustering: `tasks/speaker_clustering.py` `cluster_for_file` (`:72`), `recluster_all` (`:150`); dispatch `postprocess.py:774`; `api/endpoints/speaker_clusters.py` (`/recluster` `:98`, split, outliers, promote, merge) | cross-file clusters |
| P12 | suggestions: `services/smart_speaker_suggestion_service.py` (`:253,441`); used by speaker list `api/endpoints/speakers.py:668-899` and `GET /speakers/{uuid}/suggestions` (`speaker_profiles.py:692`, also `:505,586-598,656,784`) | read |
| P13 | admin/batch: `tasks/embedding_migration_v4.py` (`:430,606,772,958`), `tasks/embedding_consistency_repair.py` (`:107,170` re-creates missing docs), `tasks/speaker_embedding_migration.py:212`, `tasks/combined_speaker_analysis_task.py` (`:87,218`, embeddings **and** gender), `app/scripts/batch_speaker_matching.py:46` | |
| P14 | OpenSearch writes (backstop): `opensearch_service/speaker_write.py` `add_speaker_embedding_v4` (`:20`), `bulk_add_speaker_embeddings_v4` (`:120`), `add_speaker_embedding` (`:215`); `opensearch_service/profiles.py` `store_profile_embedding_v4` (`:21`), `store_profile_embedding` (`:214`) | every write lands here |
| P15 | sharing: `services/permission_service.py` `get_accessible_profile_ids` (`:482-530`, profiles reachable through shared collections), `_for_file` (`:535`) | cross-user matching candidates |
| P16 | erasure helpers: `services/gdpr_erasure_service.py` `_erase_speaker_voiceprints` (`:117`, scopes by user and/or org; no "personal only" scope) | reused by purge |

### 1.3 Gender producers and consumers

| # | Site | Today |
|---|---|---|
| G1 | `services/speaker_attribute_settings.py` `resolve_speaker_attribute_flags` (`:37-67`) | user > system > default. The system key is a **default the user can override**, not a ceiling |
| G2 | `tasks/speaker_attribute_task.py:598` | honours the per-user flag (#1200 fixed) |
| G3 | `tasks/transcription/speaker_processor.py` `apply_sidecar_gender_for_user` (`:422`), from `finalize.py:171` | honours the per-user flag |
| G4 | `transcription/diarizer_native.py:123,856` | sidecar asked for gender by the global env `DIAR_NATIVE_GENDER` only |
| G5 | `tasks/speaker_attribute_migration_task.py` `_gender_result_writer` (`:111-171`), `combined_speaker_analysis_task.py:68-70` | admin bulk: **no** per-user or org check |
| G6 | `tasks/speaker_identification_task.py` `_apply_cross_reference_context` (`:346-396`) + `services/metadata_speaker_extractor.py:441-590` | sends "voice: female 92%" to the LLM, **including remote providers**, and infers gender from first names |
| G7 | `services/speaker_clustering_service.py:1624-1654` | splits clusters by `predicted_gender` |
| G8 | API serialization `schemas/media.py:336,377`, `schemas/speaker_cluster.py:38-113`; profile consensus `speaker_profiles.py:1015`; manual `confirm-gender` (`speakers.py:1417`, `speaker_profiles.py` profile confirm) | display |
| G9 | Frontend `components/speakers/GenderBadge.svelte`, `stores/speakerAttributePrefs.ts` | display |

**Corrections.** #1280 says the gender toggle is "display only, see #1200". #1200 is closed and the
per-user toggle now gates G2/G3. The real gaps are G4 (global env only), G5, G6 and G7, and that no layer
above the user can force it off. #1132 says the per-upload `disable_diarization` flag is ignored; it is
honoured now (`media_file.requested_disable_diarization`, `tasks/transcription/dispatch.py:460`).

## 2. Design decisions

**D1. One restrict-only policy layer.** `services/privacy/policy.py` resolves
`PrivacyPolicy(voiceprints: Decision, gender: Decision)` where `Decision = (enabled: bool,
locked_by: "platform" | "instance" | "organization" | "user" | "consent" | None)`. Effective =
platform AND instance AND organization AND user (AND consent for voiceprint creation). `locked_by` names
the highest layer that turned it off. Rejected: capability keys (they describe a deployment or tier, are
resolved per request, and Celery tasks see `request=None`); reusing `speaker_attribute.*` (those are
defaults a user overrides, not ceilings); one resolver per feature (#1132's owner update asks for one
policy module, one screen, one API).

**D2. Storage.** Instance: `SystemSettings` keys `privacy.voiceprint_matching_enabled` and
`privacy.gender_inference_enabled`, defaults coded in `core/constants.py` (both `True`). No env vars;
`DIAR_NATIVE_GENDER` stays as an additional hard off for the sidecar. Organization: a typed
`organization_policy` table, one row per org (a missing row = inherit everything). User: `UserSetting`
keys `privacy_voiceprint_matching_enabled` (new) and the existing
`speaker_attribute_gender_detection_enabled`. Rejected: JSONB on `organization` (no CHECKs, and
`test_orm_ddl_divergence.py` would see opaque blobs); a key-value org table (no types, no validation).

**D3. Org redaction floor = stored floor ∪ resolver floor.** `resolve_redaction_floor` unions the
`organization_policy` floor columns with the registered resolver's result (strictest wins, the same
union `_apply_tenant_floor` already applies). Rejected: shipping core's stored floor as the default
resolver, because a derived deployment that registers its own resolver would silently drop every floor
an org admin saved.

**D4. Three enforcement tiers, fail closed.**
1. *Plan.* `PlanInputs.voiceprints_allowed` (from the file's org and owner). False gives
   `speaker_id.runs=False`, `execution="none"` and an info issue `voiceprints_disabled`. The per-job
   transcription config gets `enable_native_embeddings=False` and `request_gender=<policy>`, so the
   sidecar is never asked for gender and centroids never leave the worker.
2. *Entry gates.* Every P4-P13 producer and G2-G7 consumer calls
   `voiceprints_allowed_for_file(db, file_id)` / `gender_allowed_for_file(...)` (fresh DB read, no cache)
   and returns a "skipped by policy" result. This covers runs planned before the switch flipped.
3. *Write backstop.* The five P14 functions and `merge_speaker_embeddings` call
   `assert_voiceprint_write_allowed(user_id, organization_id)`, which uses a 10 s per-process TTL cache
   and **denies on any resolver error**. A denied write logs once per scope and returns as the
   function's existing "not written" value.

**D5. Switching off = ignore, not delete.** While voiceprints are off for a scope: no new docs; the
kNN, suggestion and cluster reads return empty; unverified voiceprint-sourced suggestions
(`speaker.suggestion_source in ('voice_match','profile_match') AND verified IS NOT TRUE`) are omitted
from responses; cluster lists return `[]`. Kept and visible: named profiles, avatars, user-verified
speaker→profile links and occurrences (user assertions, not biometric). While gender is off, inferred
gender (`gender_confirmed_by_user` false) serializes as `null`; manual confirmations stay (user-entered).
Turning the layer back on restores visibility, and new files get voiceprints again. Old files can be
backfilled with the existing "re-run speaker identification" reprocess stage. **Purge** is a separate,
explicit, audited action per layer (§3.3). One documented exception: an admin v4 embedding migration or
consistency repair run while a scope is off **skips** that scope ("no voiceprint write of any kind"),
so after the alias swap that scope has no voiceprints. Finalize reports `skipped_by_policy` and the
admin panel warns before it starts (Q3).

**D6. Consent seam (#1280) that #1132 implements, not a second gate.**
`services/privacy/consent.py`: `register_voiceprint_consent_hook(hook)`,
`VoiceprintConsentHook = Callable[[Session, VoiceprintConsentRequest], bool]`,
`VoiceprintConsentRequest(purpose: "create" | "share", subject_user_id, organization_id,
media_file_id | None, profile_ids: tuple[int, ...], requester_user_id | None)`. All registered hooks
must return `True`. No hooks = allow (current behaviour). A hook that raises = deny, logged. The session
is borrowed read-only (same contract as `RedactionFloorResolver`). Hooks must be fast: they run in
Celery tasks and in the profile-candidate query. `create` is checked inside `voiceprints_allowed_for_file`
(tiers 1 and 2). `share` is checked in P15 for non-owned candidate profiles, grouped by (owner, org), so
it runs once per owner and not once per profile. #1132 later ships the consent **record** table and
notice UI as core's own built-in hook, enabled by `privacy.voiceprint_consent_required`.

**D7. Gender inference is one switch.** It covers the voice model (G2, G5), the sidecar (G4), and
first-name inference plus the voice/name cross-reference (G6). When gender is **on** but the speaker-ID
LLM is remote (`redaction.llm_guard.is_local_provider(config)` is False), the gender cross-reference is
never added to the prompt. That keys off the provider, matching the redaction egress rule, and closes
#1132 item 6. Default stays on (Q1).

**D8. API contract when disabled.** Existing list endpoints keep their response models and return `[]`
(documented in their docstrings and OpenAPI descriptions). Mutations that would create or recompute
voiceprints (`/speaker-clusters/recluster`, split, analyze-outliers, `/admin` profile-embedding batch,
embedding re-extract) return **409** `{"detail": {"code": "voiceprints_disabled", "locked_by": "<layer>"}}`.
The reason comes from one new read endpoint, `GET /api/privacy/effective` (§3.2). Rejected: changing list
responses to objects (breaking for every client) and response headers (invisible to the axios callers).

**D9. How #1280 and #1132 combine.**

| Piece | Lands in |
|---|---|
| Policy layer, org policy table, instance/org/user switches, enforcement P1-P16 / G1-G9, consent seam, purge, org floor screen | #1280 + #1279, v0.6.0 (this plan) |
| Gender off switch + remote-LLM exclusion (#1132 item 6) | v0.6.0 (this plan) |
| Consent record + notice + `privacy.voiceprint_consent_required` (built-in hook), profile last-used/expiry, profile and account export (reuse #796), access log, data inventory page, retention per type | #1132, v0.9.0, re-scoped to reference this plan |

## 3. Changes

### 3.1 Backend, file by file

New:
- `backend/alembic/versions/v445_add_organization_policy.py`: next free number (head today
  `v444_add_takedown_notification`; re-derive with `scripts/release-tests/lib/alembic-head.py` and bump
  if another lane took it). Idempotent raw SQL, `CREATE TABLE IF NOT EXISTS organization_policy (
  organization_id INTEGER PRIMARY KEY REFERENCES organization(id) ON DELETE CASCADE,
  voiceprint_matching_enabled BOOLEAN NOT NULL DEFAULT TRUE, gender_inference_enabled BOOLEAN NOT NULL
  DEFAULT TRUE, floor_forced_categories JSONB NOT NULL DEFAULT '[]', floor_forced_pii_entities JSONB NOT
  NULL DEFAULT '[]', floor_forced_custom_words JSONB NOT NULL DEFAULT '[]', floor_toxicity_threshold
  DOUBLE PRECISION NULL CHECK (floor_toxicity_threshold BETWEEN 0 AND 1), floor_export_redacted BOOLEAN
  NOT NULL DEFAULT FALSE, floor_redact_before_llm BOOLEAN NOT NULL DEFAULT FALSE, updated_at TIMESTAMPTZ
  NOT NULL DEFAULT now(), updated_by_user_id INTEGER NULL REFERENCES "user"(id) ON DELETE SET NULL)`,
  plus `jsonb_typeof(...) = 'array'` CHECKs. Downgrade: `DROP TABLE IF EXISTS organization_policy`.
  Docstring: community has no orgs, so the table stays empty. Detection arm at the top of
  `_detect_schema_version()` (`app/db/migrations.py`) keyed on the table. Consistency test
  `tests/unit/test_v445_migration_consistency.py` (shape of `test_v37{7,8}`; use
  `assert_detected_at_or_after`, never `== REVISION`).
- `app/models/organization_policy.py` `OrganizationPolicy`; register in `app/models/__init__.py`.
- `app/services/privacy/__init__.py`, `policy.py` (resolver, `voiceprints_allowed_for_file`,
  `gender_allowed_for_file`, `assert_voiceprint_write_allowed`, `filter_shareable_profiles`,
  `set_privacy_ceiling_resolver` for the platform layer, `PrivacyCeiling(voiceprints: bool | None,
  gender: bool | None)`, community returns `None`), `consent.py` (D6), `purge.py` (§3.3).
- `app/schemas/privacy.py`: `PrivacyDecision`, `EffectivePrivacy`, `InstancePrivacyPolicy(+Update)`,
  `OrgPolicy`, `OrgPolicyUpdate`, `RedactionFloorIn/Out`, `PurgeRequest`, `PurgeAccepted`.
- `app/api/endpoints/privacy.py` (`user_router`, `admin_router`) and `app/api/endpoints/org_admin_policy.py`.
- `app/tasks/privacy_purge_task.py`: `privacy.purge_voiceprints` (CPU queue, registered via `include=`
  in `core/celery.py`; see commit `e397819f` for why a missing include is silent).

Changed:
- `core/constants.py`: the two `privacy.*` keys and defaults. `core/tenant_limits.py`
  `resolve_redaction_floor`: union the stored floor with the resolver's result (load the row on the
  borrowed session, read-only). Update the module docstring for Hook 3.
- `services/redaction/config.py:251-253`: replace the "two-argument caller" sentence (#1279 part 1) with:
  production callers thread the org from the originating record; the org floor now comes from core
  storage ∪ any registered resolver.
- `auth/constants.py`: `CLOUD_SEAM_VERSION = 10`, entry (additive): consent hook, privacy ceiling
  resolver, and the floor union semantics.
- `core/capabilities.py`: move `org.defaults` to tier 1 (server-enforced, `/org-admin/policy`, no
  platform-admin bypass) in the docstring; `tests/unit/test_capability_contract.py` follows.
- `api/router.py`: mount `org_admin_policy.router` at `/org-admin/policy` with `capability="org.defaults",
  platform_admin_bypass=False`; `privacy.user_router` at `/privacy`; `privacy.admin_router` at
  `/admin/privacy-policy` (super_admin dependency, like `redaction_settings.admin_router`).
- `auth/audit.py`: `ORG_POLICY_CHANGE = "org.policy.change"`, `PRIVACY_VOICEPRINT_PURGE =
  "privacy.voiceprints.purge"`, `PRIVACY_GENDER_PURGE = "privacy.gender.purge"`. Org-stamped (#262a).
- Pipeline (tier 1): `services/pipeline_plan.py` (`PlanInputs.voiceprints_allowed: bool = True`, issue
  code `voiceprints_disabled`), `services/pipeline_plan_inputs.py` (`organization_id` kwarg, resolve
  policy), `tasks/transcription/dispatch.py:123` (pass the file's org), per-job config
  `transcription/engine/config.py` + `transcription/config.py` (`request_gender`),
  `transcription/diarizer_native.py:856` (`"gender": _GENDER_ENABLED and job.request_gender`).
- Entry gates (tier 2): P4 `postprocess.py` (also `_dispatch_speaker_clustering`), P5, P6, P7
  `_voiceprint_stage`, P8 public methods, P9, P10, P11 tasks + endpoints (409), P12 (empty), P13 (skip with
  counts in the existing progress payloads), G2/G3 via G1, G5, G6, G7.
- G1 `resolve_speaker_attribute_flags(db, user_id, *, organization_id)`: AND the policy ceiling into
  `gender_detection_enabled`, add `gender_locked_by`. All callers pass the **file's** org from the row,
  never caller state (`_is_gender_detection_enabled` gains `file_id`).
- P15 `get_accessible_profile_ids*`: drop non-owned profiles whose owner scope has voiceprints off or
  whose `share` consent is denied (`filter_shareable_profiles`).
- Serialization (D5): the speaker/profile/cluster response builders null inferred gender and drop
  unverified voiceprint suggestions while off. Do it in one helper (`services/privacy/redact_view.py`)
  called by `schemas`-building code in `speakers.py`, `speaker_profiles.py` and `speaker_clusters.py`.
- Backstop (tier 3): the P14 functions and `speaker_maintenance.merge_speaker_embeddings`.
- `services/gdpr_erasure_service.py` `_erase_speaker_voiceprints`: add `personal_only: bool` (`must_not
  exists organization_id`) so the purge can reuse it. Erasure behaviour is unchanged.

### 3.2 API contracts

`GET /api/privacy/effective?file_uuid=<uuid?>` (any signed-in user). With `file_uuid`: access-checked
via `get_file_by_uuid_with_permission`; scope = the file's org and owner. Without: `ctx.org_id` + caller.
```json
{ "organization_uuid": "…" | null,
  "voiceprint_matching": {"enabled": false, "locked_by": "organization"},
  "gender_inference":   {"enabled": true,  "locked_by": null} }
```
`GET|PUT /api/admin/privacy-policy` (super_admin): `{"voiceprint_matching_enabled": bool,
"gender_inference_enabled": bool}`. PUT is partial and emits `ADMIN_SETTINGS_CHANGE`.

`GET|PUT /api/privacy/me` (user): `{"voiceprint_matching_enabled": bool, "locked_by": …}`. The user's own
switch; a PUT is ignored with 409 `policy_locked` when a higher layer is off.

`GET|PATCH /api/org-admin/policy` (`require_org_admin`, capability `org.defaults`; scope is always
`ctx.org_id`, there is no org parameter):
```json
{ "voiceprint_matching_enabled": true, "gender_inference_enabled": true,
  "instance": {"voiceprint_matching_enabled": true, "gender_inference_enabled": true},
  "redaction_floor": { "forced_categories": ["pii"], "forced_pii_entities": [], "forced_custom_words": [],
                       "force_toxicity_threshold": null, "force_export_redacted": false,
                       "force_redact_before_llm": true } | null,
  "updated_at": "2026-10-25T00:00:00Z" | null }
```
`redaction_floor` is `null` and PATCHing it is 404 when the `redaction.policy` capability is off for the
request. Validation (422): categories ⊆ the global floor vocabulary (`_POLICY_KEY` in
`redaction_settings.py`), PII entities ⊆ the supported list in `core/constants.py`, custom words ≤ 200
items × ≤ 100 chars, threshold in [0, 1]. Each PATCH emits `ORG_POLICY_CHANGE` (changed field names
only, no values: custom words can be sensitive).

Purge, three scopes, one body `{"targets": ["voiceprints", "inferred_gender"], "confirm":
"PURGE"}` → **202** `{"task_id": "…"}`:
- `POST /api/org-admin/policy/purge`: the caller's org.
- `POST /api/admin/privacy-policy/purge` `{"scope": "personal" | "all"}`: `all` is refused (403) in
  multi-tenant mode (any active org or `TENANCY_MODE=multi`, the #1122 test); see Q2.
- `POST /api/privacy/me/purge`: the caller's own docs in the active scope.

Each purge target is refused with 409 `policy_still_enabled` unless that target is off at the purging
layer. Otherwise new voiceprints would grow straight back.

### 3.3 Purge semantics (`services/privacy/purge.py`)

`voiceprints`: `_erase_speaker_voiceprints(scope)` on speaker and profile docs in every speaker index
(including the legacy backup index, as erasure does); `speaker_profile.embedding_count=0,
last_embedding_update=NULL`; delete `speaker_match` rows and unpromoted `speaker_cluster` (+ members) in
scope; `speaker.cluster_id=NULL`; clear unverified voiceprint suggestions. `inferred_gender`:
`predicted_gender=NULL`, the `gender` key of `attribute_confidence` and `attributes_predicted_at` where
`gender_confirmed_by_user` is not true; profile `predicted_gender=NULL`. Kept: profiles, names, avatars,
verified links. The task runs the OpenSearch delete twice, the second time 60 s later (longer than the
10 s backstop cache plus an in-flight write) to catch a write that raced the switch. It records
counts in the task row and the audit event. OpenSearch snapshots keep old docs until they rotate
(`services/opensearch_snapshot.py`); the docs page states that window.

## 4. Security and tenant isolation

| Surface | What could leak or go wrong | Guard + negative test |
|---|---|---|
| `/org-admin/policy` GET/PATCH/purge | org A admin reads/changes org B | scope only from `ctx.org_id`; test: admin of A cannot affect B's row or B's docs; member of A → 403; key off → 404 **including superuser** |
| Org purge | deletes another tenant's or personal docs | `organization_id` term in the delete query; test seeds docs for org A, org B, personal; only A's go |
| Instance purge `all` | platform admin destroys tenant data (#1122) | 403 in multi-tenant mode; test |
| `/privacy/effective?file_uuid` | probe policy of a file you cannot see | permission helper; test: other user's file → 404, same as file detail |
| Share filter (P15) | user B matches user A's profile after A opted out | test: shared collection, A off, B's suggestions exclude A's profile; control with A on |
| Backstop | resolver outage re-enables writes | deny on error; test: resolver raises → no doc written |
| Consent hook | hook bug silently allows | raise → deny; test |
| Policy staleness | write after switch-off | tier-2 fresh reads, 10 s backstop cache, double-pass purge; test flips setting mid-run (entry gate) |
| Audit | custom words copied into logs | field names only; test asserts no value in the event |
| Remote LLM | gender in prompt | test: remote config → context has no gender string; local → present (control) |

Personal scope never reads org rows. The org of a file, speaker or profile always comes from the row
(the rule in `permission_service.get_accessible_profile_ids_for_file`), never from request state.

## 5. Frontend

- New `components/settings/OrgPrivacyPolicyPanel.svelte` (lazy: loader in `$lib/settings/sectionLoaders`
  plus `LEAD_LOADER`, never a static import). Nav row `org-privacy` in the org group of
  `SettingsModal.svelte`, gated by `orgAdminCapOn(capState, 'org.defaults')`. Cards: Speaker recognition
  (toggle + purge), Inferred attributes (toggle + purge), Redaction floor (shown when `redaction.policy`
  is on). Fields the instance has turned off render disabled with "Turned off by the deployment operator".
- Extract the floor form from `RedactionPolicySettings.svelte` into `RedactionFloorForm.svelte` with a
  `mode: 'instance' | 'organization'` prop. This is the second consumer, so extraction replaces copying.
- Instance admin: a "Speaker recognition and inferred attributes" card on the Privacy & Redaction
  `policy` tab (super_admin, `privacyRedactionTabs.ts`), with the two toggles and a personal-scope purge.
- User: a "Recognize speakers across my recordings" toggle in the Speaker Identification personal tab;
  the `SpeakerAttributeSettings.svelte` gender toggle shows locked + reason when `locked_by` is set.
- Speaker surfaces: new `stores/privacyPolicy.ts` (per file, from `/privacy/effective`). Hide suggestion
  chips, the cluster inbox and "find similar" when off, and show one inline notice naming the layer.
  `GenderBadge.svelte` already renders nothing for `null`. The store must not be imported by the root
  layout or `keyMaps.ts`.
- API client: `lib/api/privacy.ts`, and `OrgAdminApi.getPolicy/updatePolicy/purge` in `lib/api/orgAdmin.ts`.
- Purge dialog: states what is deleted and kept and needs the typed word, matching the API `confirm`.
  Destructive button uses the existing danger style; cancel hovers grey.
- i18n: about 35 keys (`settings.privacyPolicy.*`, `speakers.voiceprintsDisabled.*`), real translations
  in all 12 locales (`npm run check:i18n` checks parity only). `ar` is RTL: use logical properties
  (`margin-inline`, `text-align: start`) and check toggle and dialog alignment rendered. Light/dark
  parity through the existing settings card and toggle classes. Run `npm run check:i18n` by hand,
  because a locale-only commit does not fire the hook.
- Bundle: everything new is in lazy chunks and locale packs load per locale, so root layout and closure
  impact should be ~0 B. Confirm with `scripts/check-bundle-budget.mjs` (root ~16.7/22.5 KB, closure
  ~128.8/139.3 KB gzip) before and after.

## 6. Test plan (each must be seen red first)

Prove red on a `git archive HEAD` tree (`RED="$(mktemp -d -t ot-redcheck.XXXXXX)"`, copy only the new
test files in). Behavioural tests set state directly in the DB (`system_settings` row,
`organization_policy` row via a fixture that creates the table if absent), so on HEAD they fail by
**assertion** (a suggestion or doc exists) rather than by ImportError. Pure-API tests of new modules are
red by ImportError, and that is acceptable only for those.

Backend (pytest, live stack for OpenSearch-backed ones):
1. `tests/unit/test_pipeline_plan_privacy.py::test_voiceprints_disallowed_plans_no_speaker_id`.
2. `tests/unit/test_privacy_policy.py`: the layer matrix, `locked_by` precedence, resolver error → off,
   ceiling resolver, hook raise → deny, community = unchanged.
3. `tests/integration/test_voiceprint_policy_pipeline.py`: native inline path, embedding stage, rediarize
   `_voiceprint_stage`, reassignment task. Each with the policy off → 0 docs for the file in
   `speakers_v4`/alias; control with it on → docs present.
4. `test_voiceprint_write_backstop`: direct `add_speaker_embedding` for a disabled scope → absent.
5. `tests/api/test_speaker_suggestions_policy.py`: list + `/suggestions` → `[]` when off, non-empty
   control; `/speaker-clusters/recluster` → 409 `voiceprints_disabled`; stored unverified voice
   suggestion hidden while off and visible again after re-enable (ignore, not delete).
6. `test_shared_profile_respects_owner_policy` (P15) and `test_share_consent_hook_denies`.
7. `test_consistency_repair_skips_disabled_scope` (no re-creation); `test_v4_migration_reports_skipped_by_policy`.
8. Gender: sidecar verdicts ignored with the org off and the user on; attribute task skipped; migration
   writer leaves the speaker untouched; `_apply_cross_reference_context` with a remote config → no gender
   text, local → present; serialization nulls inferred gender but the DB value survives; the cluster
   split ignores gender when off.
9. `tests/api/test_org_admin_policy.py`: 404 with `org.defaults` off (superuser too), 403 for a member,
   PATCH persists, 422 validation, audit has no values; floor end to end: the org floor masks a subtitle
   word the user's prefs leave off (pattern of `test_org_redaction_floor_threading.py`), and stored ∪
   registered resolver both apply.
10. `tests/api/test_privacy_purge.py`: tenant-scoped deletion (A, B, personal seeded), 409 while
    enabled, `all` 403 in multi-tenant mode, kept-vs-deleted rows, two-pass delete, audit event.
11. `test_v445_migration_consistency.py`; `test_capability_contract.py` update.
12. #1279 grep: `rg -n "two-argument caller" backend/app` is empty (in an existing docs-claims unit test
    if one fits, otherwise in the verification list).

Frontend (vitest): the `OrgPrivacyPolicyPanel` locked state, PATCH payload, purge needing the typed word,
nav hidden for members and when the key is off; `SpeakerAttributeSettings` locked; suggestion UI hidden
from the store; `privacyRedactionTabs` card visibility. E2E (one test, `-m settings`): super_admin turns
voiceprints off, a file shows the notice and no suggestions, then the test **restores the setting in a
finalizer** (dev-data rule). Run `python3 scripts/audit-tests.py backend/tests` and `npm run test:audit`
to 0 findings.

## 7. Work packages

| WP | Size | Depends | Files (disjoint ownership) |
|---|---|---|---|
| WP0 #1279 docstring | S | none | `services/redaction/config.py` docstring only. Mergeable now |
| WP1 Policy core | M | none | migration, `models/organization_policy.py`, `services/privacy/{policy,consent}.py`, `core/constants.py`, `auth/constants.py`, tests 2, 11 |
| WP2 Pipeline enforcement | M | WP1 | `services/pipeline_plan*.py`, `tasks/transcription/*`, `tasks/rediarize_task.py`, `tasks/speaker_embedding_task.py`, `transcription/{config,diarizer_native,diarizer}.py`, `transcription/engine/config.py`, `opensearch_service/{speaker_write,profiles,speaker_maintenance}.py`; tests 1, 3, 4 |
| WP3 Read/API/batch enforcement | M | WP1 | `api/endpoints/{speakers,speaker_profiles,speaker_clusters,speaker_update,admin}.py`, `services/{permission_service,smart_speaker_suggestion_service,speaker_matching_service,profile_embedding_service,speaker_clustering_service}.py`, `services/privacy/redact_view.py`, `tasks/{speaker_clustering,speaker_merge_task,embedding_migration_v4,embedding_consistency_repair,speaker_embedding_migration}.py`, script; tests 5-7 |
| WP4 Gender | M | WP1 | `services/speaker_attribute_settings.py`, `tasks/{speaker_attribute_task,speaker_attribute_migration_task,combined_speaker_analysis_task,speaker_identification_task}.py`, `tasks/transcription/speaker_processor.py`, `services/metadata_speaker_extractor.py`; test 8. WP2 owns the per-job `request_gender` plumbing and WP3 owns G7 (`speaker_clustering_service.py`) and gender serialization in `redact_view.py`; both call WP1's `gender_allowed_for_file` |
| WP5 Admin/org API + purge + floor | M | WP1 | `api/endpoints/{privacy,org_admin_policy}.py`, `schemas/privacy.py`, `services/privacy/purge.py`, `tasks/privacy_purge_task.py`, `core/celery.py`, `core/tenant_limits.py`, `core/capabilities.py`, `api/router.py`, `auth/audit.py`, `services/gdpr_erasure_service.py`; tests 9, 10 |
| WP6 Frontend | M | WP5 contract | §5 files; vitest + e2e |
| WP7 Docs | S | WP2-6 | docs-site privacy controls page (seed of #1132's data inventory), `services/search/CLAUDE.md` (backstop), `services/redaction/CLAUDE.md` (floor storage), `docs/design/v060_downstream_changes.md` (seam v10), CHANGELOG |

Order: WP0 any time. WP1, then WP2/WP3/WP4/WP5 as four parallel agents in **separate worktrees** (file
sets above are disjoint; `speaker_clustering_service.py` is WP3's). WP6 can start against §3.2 as soon as
WP5's schemas land. WP7 last. Only one writer commits per checkout (root CLAUDE.md).

## 8. Rollout and compatibility (derived deployments)

- Upgrade is behaviour-neutral: empty table, both instance keys default on, no hooks, no ceiling.
  Existing voiceprints and genders are untouched.
- Seam v10 is additive: `register_voiceprint_consent_hook`, `set_privacy_ceiling_resolver`, and the floor
  union. A deployment that already supplies org floors from its own tables keeps working; both floors
  apply and the strictest wins. Its resolvers run inside Celery tasks with no request, so they must
  answer per organization and not per request.
- A capability resolver must now return `org.defaults` explicitly to show the org panel (resolver results
  fail closed). Omitting it hides and 404s the panel, which is the safe default.
- A deployment that replaces `$lib/cloud` gets the org panel from core (it is not an overlay component).
  It is visible only when `isCloudEdition`, the key is granted with audience `org_admin`, and the user is
  `org:admin`.
- Rollback: the downgrade drops the table (org choices are lost; instance keys stay). Purges are
  irreversible by design.

## 9. Verification and exit criteria

1. `./scripts/run-dev-tests.sh --full` green; `scripts/safe-precommit.sh run --all-files` and
   `--hook-stage pre-push` green; `cd frontend && npm run check:i18n && npm run check && npm run build`.
2. Live stack, policy off for a test org: upload a multi-speaker file. The OpenSearch speaker indices have
   no docs with that `media_file_id`; the sidecar request body has `"gender": false`; the file shows the
   notice and no suggestions; the speaker-ID LLM prompt (`mock-echo`) has no gender text.
3. Turn it back on: the earlier stored suggestion is visible again. Purge: the counts match the seeded
   docs and the other tenant's docs survive.
4. Org admin screen opened in a browser in light, dark and `ar`; floor saved, then a subtitle export is
   masked.
5. `rg -n "two-argument caller" backend/app` is empty. Bundle budget unchanged within noise.
6. #1132's body updated to point at this plan, with D9's split; #1280 and #1279 closed by the PRs, one
   `Closes` keyword per issue.

## 10. Open questions (owner)

1. **Gender inference default.** Recommended: keep it **on** in v0.6.0 (behaviour-preserving), and flip
   the instance default with #1132's notices in v0.9.0.
2. **Instance purge `all` in multi-tenant mode.** Recommended: refuse. Org purges belong to org admins,
   and the platform uses the erasure tooling, consistent with #1122.
3. **Admin embedding migration while a scope is off** drops that scope's voiceprints (D5). Recommended:
   accept, with a pre-start warning and `skipped_by_policy` in the finalize report, rather than carrying
   biometric docs the policy forbids writing.
