# backend/app/schemas — Pydantic v2 wire contract

## Purpose

Request/response shaping and validation only; logic lives in `app/services`. 25 modules,
~4.9k lines. This is where the hybrid-ID rule and the "thin frontend" contract are enforced.

## Key files

- `base.py` — `UUIDBaseSchema`: sets `from_attributes=True` **plus** a `@model_validator(mode="before")`
  that strips the internal integer `id` and rewrites the `user` / `media_file` / `speaker` / `profile` /
  `shared_by_user` relations into `*_id` **UUIDs**. Inherit it for anything validated from an ORM row.
- `media.py` (834 lines, the outlier) — MediaFile, Speaker, TranscriptSegment, Tag, Comment, Task,
  Analytics, Collection + 3 local enums. `validate_whisper_model` gates on `VALID_LOCAL_WHISPER_MODELS`
  (L18); the whisper-model / `min<=max` / positive-speaker validator trio is duplicated verbatim on
  `ReprocessRequest` (L92+) and `PrepareUploadRequest` (L198+).
- `media_source.py:22` — `_validated_hostname` is an **SSRF gate**, not a format check: the value
  becomes an allowed host that `protected_media_plugins/mediacms.py` fetches server-side. It
  delegates to `app/utils/url_validation.is_safe_url`, which resolves the name and rejects on
  address class. It used to be a regex + a "must contain a dot" rule + a hardcoded first-label
  blocklist that resolved nothing and accepted `169.254.169.254`, `127.0.0.1` and `0.0.0.0`
  (audit A1). **Never reintroduce a local host-safety check here** — `url_validation` is the one
  implementation. ⚠️ `admin.py:158-205` still re-declares the same shapes with a **regex only**;
  that path is admin-only and is covered at request time by `mediacms.py`'s
  `assert_safe_outbound_url` calls, but the schema itself is unguarded.
- `__init__.py` — a **partial, drifting** re-export; 11 modules are absent (`base`, `admin`, `search`,
  `watch_source`, `speaker_cluster`, `topic`, `redaction_settings`, …). Import from the submodule.
  `UserSchema` / `GroupSchema` are aliases that exist only here.

## Conventions / patterns

- Naming is **not uniform**. Core domain files use `XBase`/`XCreate`/`XUpdate`/**`X`** — the bare name
  *is* the response schema (there is no `MediaFileResponse`). Newer peripheral files use explicit
  `XResponse`. Settings modules use `X`/`XUpdate`/`XSystemDefaults`. `search.py` suffixes `…Schema`.
  Match the file you're in. `*Update` fields are all-Optional (except `sharing.py:20`, `group.py:33`).
- ORM config is written three ways — `ConfigDict(...)`, raw `{"from_attributes": True}`, and v1-style
  `class Config:` (`auth_config`, `download_settings`, `transcription_settings`). All work.
- **No aliases anywhere**: zero `Field(alias=)`, `serialization_alias`, `populate_by_name`. Wire names
  always equal Python names.
- **Enums are local, not shared.** Only `media.py:14` imports from `core/enums` (`FileStatus`,
  re-export for back-compat); `ASRProvider`, `LLMProvider`, `SourceType`, `TaskStatus` etc. are each
  declared in their own schema module. Defaults for DB-backed settings come from `core/constants.py`.

## How it connects

- **Pre-formatted display fields — fat backend, thin frontend.** `MediaFile` L475-496:
  `formatted_duration|upload_date|file_age|file_size`, `display_status`, `status_badge_class`,
  `speaker_summary`, `error_category`/`error_suggestions`, `is_retryable`. `TranscriptSegment` L365-370:
  `formatted_timestamp`, `display_timestamp`, `speaker_label` (always the raw `SPEAKER_01`, for colour
  stability), `resolved_speaker_name`. `Task` L671-673: `age_category`, `formatted_duration`,
  **`status_display`** (inverted name vs `MediaFile.display_status`). Producers:
  `services/formatting_service.py` — `format_media_file()` L158, `format_transcript_segment()` L213.
  **A new computed display value belongs there, never in the SPA.** Two exceptions: `Speaker`'s
  `computed_status`/`status_text`/`status_color`/`resolved_display_name` come from `SpeakerStatusService`,
  and nothing populates `Task.age_category`/`status_display`.
- TS mirrors are **hand-written — no codegen** in `frontend/package.json`:
  `frontend/src/lib/types/{media,speaker,summary,speakerCluster,groups}.ts` and
  `frontend/src/lib/api/*.ts` (asrSettings, llmSettings, authConfig, watchSourcesApi, redactionSettings,
  transcriptionSettings, downloadSettings, prompts). `TranscriptSegment` has two competing TS copies
  (`stores/transcriptStore.ts:3`, `lib/utils/scrollbarCalculations.ts:6`).

## `auth_config.py` is not documentation — it is the write contract

The per-category models at the bottom of `auth_config.py` are the **allow-list, type contract
and bounds** that `PUT /admin/auth-config/{category}` validates against, the source of
`AuthConfigService.CONFIG_CATEGORIES`, and the source of the coded default a malformed stored
value falls back to. They were dead code while the write path accepted a bare `dict[str, Any]`.

- `_CategoryConfig` sets `extra="forbid"`, which is what turns a typo'd key
  (`oidc_verify_audiance`) into a 400 instead of a row stored forever and read by nothing.
- **Every field must have a default** — the write path validates *partial* payloads, so a
  required field would make an unrelated single-field save fail.
- **No config key may appear in two categories.** `auth_config.config_key` is globally UNIQUE,
  so a key claimed by two tabs lands in whichever wrote it first and then goes missing from the
  other tab's GET. `tests/unit/test_auth_config_validation.py` pins both invariants.
- `CROSS_FIELD_KEYS` + `_check_cross_field_rules` reject combinations that are individually
  valid but jointly incoherent (`allow_registration` while `local_enabled` is off;
  `pki_verify_revocation` with no `pki_ca_cert_path`). The caller merges the payload over the
  *current effective* values first, so the rejected state cannot be assembled one save at a time.
  The PKI rule exists because `core/config.py:_validate_pki_settings` enforces the same
  invariant at startup for the **`.env`** spelling only; once those keys became live (issue
  #498) the admin UI could otherwise assemble a state the environment refuses to boot with.
  **A cross-field rule mirroring a startup validator needs a copy here** — the two config
  sources are validated by different code.
- `CATEGORY_SCHEMAS` iteration order is the admin UI's tab order. Add a category in exactly one
  place.
- `AuthConfigResponse.is_set` is how a sensitive key is reported: `config_value` is always
  `None` on the wire. **Do not reintroduce a `***REDACTED***` placeholder** — the panel bound it
  into the password field and the next save encrypted it over the real secret.
- `group_mapping.py` and `invitation.py` enforce their own invariants at the wire edge as well
  as in the service and the database, because each layer is reachable without the others:
  `grants_role` is regex-capped at `user|admin`, and a mapping must grant a group, a role, or
  both.

## Gotchas

- **The hybrid-ID rule (`base.py:26-30`) is broken in exactly two places.** `auth_config.py:36,50`
  expose `id: int` (served at `api/endpoints/auth_config.py:61,279`, mirrored in
  `frontend/src/lib/api/authConfig.ts:7`); `admin.py:271-275` `QuarantinedFile` exposes `user_id`,
  `organization_id`, `quarantined_by` as ints. Don't copy either. `Task.id` is a *string* Celery id by
  design; `TranscriptSegmentUpdate.id` is request-only.
- **Some schemas were dead documentation and have been deleted (issue #567 / E8, 2026-08-24).**
  `api/endpoints/search.py` declares no `response_model` at all — it hand-builds a dict instead —
  so most of `search.py`'s response schemas were never actually wired to anything. E8 enumerated
  the whole package mechanically (a class is dead iff it has ≤1 whole-repo reference AND is not a
  base class of anything else in `schemas/` AND is not in `__init__.py`'s imports/`__all__`) and
  deleted exactly 30 classes across 7 files, 8 of them in `search.py`
  (`SuggestionItemSchema`, `FilterOptionsSchema`, `ReindexRequestSchema`, `ReindexStatusSchema`,
  `EmbeddingModelsResponseSchema`, `NeuralModelsResponseSchema`, `NeuralModelStatusSchema`,
  `SetActiveNeuralModelSchema`). The rest of `search.py` (`SetEmbeddingModelSchema`,
  `SearchOccurrenceSchema`, `SearchHitSchema`, `SearchResponseSchema`, `EmbeddingModelSchema`,
  `NeuralModelInfoSchema`, `SummarySectionMatchSchema`, `SummaryHitSchema`) legitimately
  survives the same method — each is referenced as a nested field type by a sibling class in the
  same file, which is real usage even though the endpoint itself never puts any of them behind a
  `response_model`. ⚠️ **A prior version of this note also named `CustomVocabularyResponse`,
  `UserASRSettingsResponse`, `ASRSettingsList`, and `SpeakerClusterResponse` as dead — that was
  wrong.** All four are re-exported through `schemas/__init__.py`'s `__all__` (E8's method
  excludes anything re-exported for exactly this reason — a re-export is a real, if indirect,
  consumer), and `UserASRSettingsResponse`/`SpeakerClusterResponse` are also referenced as
  nested field types by other classes in their own files.

  **Phase B (2026-08-24) found and deleted 6 more**, all backend-only:
  `speaker_cluster.py`'s `OutlierAnalysisResponse`/`BatchVerifyResponse`/`ReclusterResponse`
  and `summary.py`'s `SpeakerInfo`/`ContentSection`/`SummaryMetadata` (the last three carried a
  stale `# Legacy schemas kept for backward compatibility` comment — nothing actually read
  them). **E8's original pass missed these because its `rg` hit-count wasn't scoped to
  `backend/`**: `frontend/src/lib/types/speakerCluster.ts` and `.../types/summary.ts` declare
  hand-authored TypeScript mirrors of the identical names, which pushed the whole-repo hit
  count above E8's ≤1 threshold and hid genuinely dead Python classes behind an unrelated
  same-named TS type. **Re-running this enumeration must scope the reference count to
  `backend/`, or count only Python-file hits** — a same-named frontend type is not a Python
  consumer. Deleting `OutlierAnalysisResponse` leaves `MinorityAnalysisItem` (same file) as a
  fresh second-order orphan — its only remaining reference was the field type usage inside the
  now-deleted class — deliberately left alone per the same "don't chase in the same pass"
  discipline as the first round.

  Re-running the (corrected) enumerator after these 6 deletions surfaces further second-order
  orphans on top of the original 5 — deliberately not chased in the same pass; check the
  audit-remediation branch's Phase B re-audit notes before assuming this list is now
  exhaustive.
- **There is exactly one tag shape (#326).** `media.py:Tag` (`uuid`/`name`/`source`) is what
  `/api/tags` (as `TagWithCount`), `POST /api/tags`, `POST /api/tags/files/{uuid}/tags` **and**
  `MediaFileDetail.tags` all serve. `MediaFileDetail.tags` was `list[str]` until #326 — don't
  reintroduce a name-only projection; `source` is the AI-vs-manual signal and names are unique
  only per owner since `v374_add_tag_user_id`. `MediaFile` (the **list** schema) has no `tags`
  field at all, and adding one costs a per-row query. Note the OpenSearch index is a separate
  contract: search hits still carry `tags` as a `keyword` array of names.
- **`ConnectionTestResponse` is defined twice** (`llm_settings.py:152`, `watch_source.py:293`) and
  `__init__.py` exports only the LLM one — the package-level import silently yields the wrong shape.
- `sharing.py:24 Share.shared_by` is typed `UserBrief` but `UUIDBaseSchema` maps `shared_by` to a bare
  UUID — safe only because `Share` is built from explicit kwargs, never `model_validate(row)`.
