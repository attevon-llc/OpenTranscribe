# backend/app/schemas — Pydantic v2 wire contract

## Purpose

Request/response shaping and validation only; logic lives in `app/services`. 42 modules,
~8.5k lines (`wc -l backend/app/schemas/*.py`). This is where the hybrid-ID rule and the "thin frontend" contract are enforced.

## Key files

- `base.py` — `UUIDBaseSchema`: sets `from_attributes=True` **plus** a `@model_validator(mode="before")`
  that strips the internal integer `id` and rewrites the `user` / `media_file` / `speaker` / `profile` /
  `shared_by_user` relations into `*_id` **UUIDs**. Inherit it for anything validated from an ORM row.
- `media.py` — MediaFile/MediaFileDetail, Comment, Task, Analytics, `CollectionResponse`, subtitle
  validation. The domain schemas it embeds live beside it: `speaker.py` (Speaker, profiles,
  `split_attribute_confidence`), `transcript.py` (segments + paging), `tag.py`, `collection.py`
  (imported by `media.py`, never the reverse), and `processing_request.py` (`ReprocessRequest`,
  `PrepareUploadRequest`; `validate_whisper_model` gates on `VALID_LOCAL_WHISPER_MODELS`; the
  whisper-model / `min<=max` / positive-speaker validator trio is duplicated verbatim on both).
- `media_source.py:22` — `_validated_hostname` is an **SSRF gate**, not a format check: the value
  becomes an allowed host that `services/protected_media_plugins/mediacms.py` fetches server-side. It
  delegates to `app/utils/url_validation.is_safe_url`, which resolves the name and rejects on
  address class. It used to be a regex + a "must contain a dot" rule + a hardcoded first-label
  blocklist that resolved nothing and accepted `169.254.169.254`, `127.0.0.1` and `0.0.0.0`
  (audit A1). **Never reintroduce a local host-safety check here** — `url_validation` is the one
  implementation. ⚠️ `admin.py`'s `validate_hostname` validators (~L150-200) still re-declare the same shapes with a
  **regex only**; that path is admin-only and is covered at request time by `mediacms.py`'s
  `resolve_pinned_target` (`utils/media_origin._pin_media_url`), but the schema itself is unguarded.
- `__init__.py` — a **partial, drifting** re-export; 28 of the other modules are absent (`base`, `admin`, `search`,
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
- **Enums are local, not shared.** Few schemas import from `core/enums` (`media.py` imports `FileStatus`;
  `post_processing.py`, `llm_settings.py` and `takedown_notice.py` import the enums their wire shapes carry);
  `ASRProvider`, `SourceType`, `TaskStatus` etc. are each declared in their own schema module. A value set
  that a service also dispatches on (`LLMProvider`) lives once in `core/enums` and both sides import it. Defaults for DB-backed settings come from `core/constants.py`.

## How it connects

- **Pre-formatted display fields — fat backend, thin frontend.** `MediaFile` (the fields near `formatted_duration`):
  `formatted_duration|upload_date|file_age|file_size`, `display_status`, `status_badge_class`,
  `speaker_summary`, `error_reason`/`error_suggestions`/`user_message`, `is_retryable`
  (`error_reason` is the renamed wire field, issue #843 — never `media_file.error_category`,
  which is the retry-policy column and is not on the wire at all). `TranscriptSegment`:
  `formatted_timestamp`, `display_timestamp`, `speaker_label` (always the raw `SPEAKER_01`, for colour
  stability), `resolved_speaker_name`. `Task`: `age_category`, `formatted_duration`,
  **`status_display`** (inverted name vs `MediaFile.display_status`). Producers:
  `services/formatting_service.py` — `format_media_file()`, `format_transcript_segment()`.
  **A new computed display value belongs there, never in the SPA.** Two exceptions: `Speaker`'s
  `computed_status`/`status_text`/`status_color`/`resolved_display_name` come from `SpeakerStatusService`,
  and nothing populates `Task.age_category`/`status_display`.
- TS mirrors are **hand-written — no codegen** in `frontend/package.json`:
  `frontend/src/lib/types/{media,speaker,summary,speakerCluster,groups}.ts` and
  `frontend/src/lib/api/*.ts` (asrSettings, llmSettings, authConfig, watchSourcesApi, redactionSettings,
  transcriptionSettings, downloadSettings, prompts). `TranscriptSegment` has two competing TS copies
  (`stores/transcriptStore.ts`, `lib/utils/scrollbarCalculations.ts`).

## `auth_config.py` is not documentation — it is the write contract

The per-category models at the bottom of `auth_config.py` are the **allow-list, type contract
and bounds** that `PUT /admin/auth-config/{category}` validates against, the source of
`auth_config_keys.CONFIG_CATEGORIES`, and the source of the coded default a malformed stored
value falls back to. They were dead code while the write path accepted a bare `dict[str, Any]`.

- `_CategoryConfig` sets `extra="forbid"`, which is what turns a typo'd key
  (`oidc_verify_audiance`) into a 400 instead of a row stored forever and read by nothing.
- **Every field must have a default** — the write path validates *partial* payloads, so a
  required field would make an unrelated single-field save fail.
- **No config key may appear in two categories.** `auth_config.config_key` is globally UNIQUE,
  so a key claimed by two tabs lands in whichever wrote it first and then goes missing from the
  other tab's GET. `tests/unit/test_auth_config_validation.py` pins both invariants.
- `CROSS_FIELD_KEYS` + `_check_cross_field_rules` (both in `auth_config_validation.py`) reject combinations that are individually
  valid but jointly incoherent (`allow_registration` while `local_enabled` is off;
  `pki_verify_revocation` with no `pki_ca_cert_path`). The caller merges the payload over the
  *current effective* values first, so the rejected state cannot be assembled one save at a time.
  The PKI rule exists because `core/auth_settings_checks.py:validate_pki_settings` enforces the same
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

- **The hybrid-ID rule (`base.py`'s `UUIDBaseSchema` docstring) is broken in one known place.**
  `auth_config.py`'s `AuthConfigResponse` and `AuthConfigAuditResponse` expose `id: int` (served by
  `api/endpoints/auth_config.py`, mirrored in `frontend/src/lib/api/authConfig.ts`). `admin.py`'s
  `QuarantinedFile` used to expose integer `user_id`/`organization_id`/`quarantined_by` too; it now
  carries `owner_uuid`/`organization_uuid`/`quarantined_by_email` (issue #576). Don't copy the former. `Task.id` is a *string* Celery id by
  design; `TranscriptSegmentUpdate.id` is request-only.
- **Dead schemas get deleted, including second-order orphans (#567, v0.7.0 L04).** A response
  schema no endpoint puts behind a `response_model` and no sibling nests is dead documentation.
  `scripts/guards/check_vulture.py` flags an unused class; deleting one can orphan the classes it
  nested, so re-run it until it reports nothing (`search.py` and `speaker_cluster.py` shrank this
  way). A frontend type of the same name is a hand-written mirror, not a Python consumer: count
  references under `backend/` only. A re-export in `schemas/__init__.py` is not a consumer either.
- **There is exactly one tag shape (#326).** `tag.py:Tag` (`uuid`/`name`/`source`) is what
  `/api/tags` (as `TagWithCount`), `POST /api/tags`, `POST /api/tags/files/{uuid}/tags` **and**
  `MediaFileDetail.tags` all serve. `MediaFileDetail.tags` was `list[str]` until #326 — don't
  reintroduce a name-only projection; `source` is the AI-vs-manual signal and names are unique
  only per owner since `v374_add_tag_user_id`. `MediaFile` (the **list** schema) has no `tags`
  field at all, and adding one costs a per-row query. Note the OpenSearch index is a separate
  contract: search hits still carry `tags` as a `keyword` array of names.
- **`speaker.attribute_confidence` is a mixed JSONB bag; `Speaker.attribute_confidence` is not
  (#1026).** Voice-attribute prediction stores numbers (`gender`), but speaker identification
  writes `metadata_hints` (a list of `{name, role, confidence, source}`) and `alignment` /
  `alignment_hint` (strings) into the same column. The schema serves only the numeric scores and
  exposes the rest as `metadata_hints` / `gender_alignment` / `gender_alignment_hint`, via
  `split_attribute_confidence` — which the hand-built speaker LIST dict also calls. Typing the
  field as the raw bag's `dict[str, float]` 500'd file detail, segments and speaker detail for
  every file with a hint. A new key written into that column needs a line in the split.
- **`ConnectionTestResponse` is defined twice** (`llm_settings.py`, `watch_source.py`) and
  `__init__.py` exports only the LLM one — the package-level import silently yields the wrong shape.
- `sharing.py` `Share.shared_by` is typed `UserBrief` but `UUIDBaseSchema` maps `shared_by` to a bare
  UUID — safe only because `Share` is built from explicit kwargs, never `model_validate(row)`.
