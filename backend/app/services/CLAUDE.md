# app/services — business-logic layer

## Purpose

Everything between the API/Celery entry points and the data layer: well over 100 top-level modules plus subpackages (`ls` this directory); endpoints and
tasks should stay thin and call in here. `interfaces.py` declares the structural `Protocol`
contracts (`StorageService`, `SearchService`, `CacheService`, `NotificationService`) that
`minio_service`, `opensearch_service`, `redis_cache_service`, and `utils/websocket_notify`
already satisfy — depend on the Protocol, not the concrete module, at new seams.

## Where things live

- **User-facing error classification** — `error_categorization_service.py`. `UserErrorReason`
  is the sole user-facing vocabulary (wire field `error_reason`), distinct from
  `app.utils.error_classification.ErrorCategory` (retry policy, `media_file.error_category`,
  never on the wire). No-raw-echo contract (issue #786): every `user_message` and suggestion
  is a FIXED sentence — the raw exception never reaches a client. Classify ONCE, at the
  failure site (#959): a failure handler calls `ErrorCategorizationService.classify_failure(raw)`,
  stores `user_message` in `last_error_message`/`task.error_message` and `retry_category` in
  `error_category`; the raw text goes to the ERROR log only. Retry policy reads
  `stored_category(media_file.error_category)` — never `categorize_error` over stored prose.
  Read edges use `user_message_for` / `error_fields_for`; `scripts/audit-error-disclosure.py`
  (pre-commit) fails on an un-sanitized edge or a prose re-derivation.
  `tasks/transcription/notifications.py::send_error_notification` is the one chokepoint every
  failure-notification caller routes through.
- **Search / retrieval** — `search/` (transcript chunks + hybrid/neural; **has its own
  CLAUDE.md** with the critical `cosinesimil` score gotcha), `opensearch_service/` (the
  speaker/voiceprint kNN plane + file docs; a package since #284 A3.5 — `client` owns the
  singleton, `aliases`/`indices`/`repair` the index plane, `speaker_*` the documents, and
  `matching`/`profiles`/`clusters` the kNN reads. Its `__init__` re-exports every name the
  old flat module exported), `opensearch_snapshot.py`, `similarity_service.py`.
  (`opensearch_summary_service.py` is **gone** — the `transcript_summaries` index it owned
  is retired, #67. A summary's SOURCE OF TRUTH is `media_file.summary_data` and nowhere
  else; the only code that still names the retired index purges legacy documents, in
  `opensearch_erasure.py` and `tasks/opensearch_integrity_task.py`. Since #963 a summary is
  ALSO indexed as a derived, rebuildable `doc_type: "summary"` plane inside `transcript_chunks`
  — never a second store of record; see `search/CLAUDE.md`'s "Index v6" section.)
- **Speakers** — `speaker_*_service.py`, `profile_embedding_service.py`,
  `smart_speaker_suggestion_service.py`, `embedding_mode_service.py`,
  `metadata_speaker_extractor.py`. **Any code that deletes a speaker or moves one out of a
  cluster or profile goes through `speaker_cleanup.py`** — see "Removing a voice" below. Clustering is `speaker_clustering_service.py` + `clustering/` (its CLAUDE.md).
- **Privacy policy** — `privacy/` (#1280) is the ONE answer to "may voiceprints exist / may gender be inferred" for a
  (user, organization): `policy.py` ANDs platform (`set_privacy_ceiling_resolver`), instance (`SystemSettings` `privacy.*`),
  organization (`organization_policy` row; none = inherit) and user (`UserSetting`), and every error path DENIES. Call
  `voiceprint_decision` / `gender_decision` (or the `*_for_file` forms, fresh read) in a producer or consumer, `gates.py` in a
  Celery stage (`voiceprints_allowed(file_id)`, `gender_allowed`, `job_privacy_overrides`), `redact_view.py` when SHAPING a
  response, and `assert_voiceprint_write_allowed` only inside OpenSearch speaker writes (10 s cache). `consent.py` is the seam a
  consent implementation registers a hook on (all hooks must return `True`; a raise denies). Switching a layer off HIDES, only
  `purge.py` deletes, and only once the switch is off. A purge is MEASURED: `purge_voiceprints.py` (relational half + OpenSearch
  documents by the Postgres ids of the scope; an ORGANIZATION purge also deletes and measures the documents stamped with its
  `organization_id` (`purge_voiceprints.org_stamp_clause`: orphans and integer-keyed legacy documents have no row), a personal or
  user purge never deletes by stamp; a pass hands the cluster centroid ids it could not confirm gone to the next pass as
  `carried_cluster_uuids`, because their rows are already deleted; "outside the scope" is `not_in_scope`, a NOT EXISTS anti-join,
  never `NOT IN (SELECT ...)`), `purge_residual.py` (a fresh-session,
  post-refresh check whose values must all be 0; unmeasurable = unverified, never clean), `purge.run_purge_pass` returns a
  `PassResult` with exact counts per store, and the task fails the purge on a final residual. It also reverts what a voiceprint
  named (`speaker.identity_source`, below) and never touches `human`/NULL identities or another tenant's rows.
  `policy_admin.py` is the write side the routes share. Never re-read `speaker_attribute.*` or a user toggle to decide gender:
  `speaker_attribute_settings.resolve_speaker_attribute_flags(..., organization_id=)` already ANDs the policy ceiling; pass the
  FILE's organization from its row. `SpeakerMatchingService` does that itself for a call that names a speaker
  (`find_and_store_speaker_matches`: the organization argument is only a hint that can narrow, never widen), and refuses
  an embedding-only call (`match_speaker_to_*`) that states no `organization_id`, because omitted never means personal.
  The speaker-ID LLM never gets the gender cross-reference from a non-local provider
  (`speaker_identification_task._with_gender_context`). A speaker's tenant is its FILE's (`media_file.organization_id`):
  `speaker.organization_id` is never stamped by the pipeline, so purge and every reader join through the file
  (`PurgeScope.speaker_filters`, the profile rename, `require_speaker_access`, the segment speaker assignment; `tests/unit/test_speaker_tenant_discipline.py` fails on any new read of the speaker row's own column), and every writer that rebuilds a speaker document passes the file's organization
  (`PreparedFile.organization_id`, the merge plan's `organization_id`) or the voiceprint reads as personal. A removal
  or erasure is never gated by the switch (`speaker_cleanup.profile_move_impact` drops only the aggregate a voice
  would JOIN while off). **Per-file layer (#1356):** `media_file.voiceprint_consent` (`unspecified|granted|declined`, v454) is a restrict-only layer checked by `policy._file_consent_allows` AFTER the stored layers and BEFORE the consent hooks (`locked_by="file"`); `assert_voiceprint_write_allowed(..., media_file_id=)` re-reads it uncached on every write and hands the hooks the file id; `policy.declined_file_ids(db, ids)` is the ONE exclusion rule every cross-file AVERAGE applies (profile embedding, cluster centroid, recluster, merge, v4 profile backfill), policed by `tests/unit/test_voiceprint_average_sites_honour_file_consent.py` (derives the averaging modules from the code). The write side is `privacy/file_consent.py`: prepare sets, complete only restricts, the owner's `PATCH /files/{uuid}/voiceprint-consent` commits `declined` and then queues `purge_reason="opt_out"` through `policy_admin.start_purge`. `granted` is the uploader's recorded claim, never verified. User docs: `docs-site/docs/features/privacy-controls.md`.
- **Collection visibility** — `collection_visibility.py` is the one place that answers "which
  collections may this caller see, and which files does that make readable" (#1051, #1275).
  `collection_permission_ranks` / `collection_permission` are the ONE statement behind
  `PermissionService.get_collection_permission` and `get_accessible_collection_ids` (an org
  collection by membership, a personal one by ownership, either by a share). On top of it:
  `visible_collection_ids` / `visible_collections_by_uuid` (what a surface may name),
  `usable_collections` (what a file may be put into: editor on a collection of the tenant, one
  statement, used by uploads, URL/YouTube/watch imports and auto-label), `collections_on_files`
  (batched, constant queries per page; the file page and the search card), `resolve_collection_filter`
  (a hidden collection filters to nothing, never 403/404), and `org_collection_file_ids_stmt`:
  **membership of an organization collection you may see is a file-READ route** (decision D-3), which
  `get_file_permission` (answers `viewer`) and `get_accessible_file_ids_subquery` (gallery, tags,
  chat scope) both include. The index side of the same route is
  `search/access_filter.access_filter_clause` (see `search/CLAUDE.md`). Don't add a raw
  `Collection` join beside any of this: `tests/unit/test_collection_visibility_discipline.py`
  fails on one that has no written reason. Never answer a hidden collection with a different
  status than a missing one (404, or an empty page).
- **Content audit** — `content_audit.py` is the ONE write path for content-access and governance
  audit events (#1277): `record_content_event(db, request, event=, actor=, resource=AuditedResource.file(row),
  details=, dedup=)`. It stamps `organization_id`/`tenant_scope` from the RESOURCE (never the actor's role), sets
  `target_user_id` to the owner when that is not the actor, drops the three opt-in events
  (`content.file.view`, `content.media.access`, `content.export`) unless the `audit.content_access`
  capability AND the super_admin setting `audit.content_access.enabled` are both on, collapses a repeat
  view / stream URL per `(event, actor, resource, surface)` for 900 s with Redis `SET NX EX` (Redis down
  writes the event), never collapses downloads or exports, never raises, and never carries a name, title,
  text or reason in `details`. Don't call `audit_logger.log` directly for a content event.
  Routes call `record_file_event` (a row or a uuid; the uuid is resolved only after the opt-in gate, so
  the file page pays no query when auditing is off) after the read or the content exists, never before a
  refusal. `AuditedResource.conversation` covers chat exports. Membership changes
  (`collection.membership.change`) are governance events: always written, after the commit.
  `tests/api/test_content_audit_events.py` pins one event per emit site.
- **Holds** — `hold_guard.py` is the ONE answer to "is this file held by anyone"
  (platform `media_file.legal_hold` OR an active organization `content_hold`): `hold_state` /
  `hold_states` per file, `held_pred` / `not_held_pred` inside a query, `is_held(file)` for a loaded
  row, `refusal_detail` for the 409 body (everyone learns `held_by`; only an org admin gets the hold
  uuids; the reason never appears). Never read `MediaFile.legal_hold` to decide whether to destroy
  or restart a clock (`test_hold_guard_single_source.py` fails on it). `content_hold_service.py`
  places and releases holds (`place_file_hold`, `place_collection_hold`, `release_hold`);
  it never commits, and what must follow the commit (storage object
  lock, the erasure-sweep nudge, the audit event) is a `HoldChange` that `finish_hold_change`
  performs. `content_hold_queries.py` holds the read-side questions.
- **Org audit view** — `org_audit_view.py` resolves `actor_display` / `target_display` /
  `resource_display` for the organization admin's audit screen at READ time (three queries per page), and
  only inside the org: a non-member (left, operator, failed login) or another tenant's / personal /
  deleted resource gets `None`, never a label. Names are never stored in an event. `member_id_for_uuid`
  answers `None` for both an unknown uuid and a non-member's, so a 403 on it is not an existence oracle.
- **Providers** — `asr/` and `diarization/`: `base.py` + `types.py` + `factory.py` + one file
  per vendor. Add a provider by adding a module and registering it in the factory; never
  branch on provider name at a call site.
- **Deterministic ingest artifacts** — `ingest_artifacts/` (**own CLAUDE.md**): per-file
  statistics, a sectioned extractive digest with provenance, and keyphrases, all with **no
  LLM and no OpenSearch**. It is what gives an `LLM_PROVIDER`-empty deployment a summary
  tier (#403 D6), and the digest sizing is derived from a *measured* 128-wordpiece
  embedding window, not from the plan's assumed 256.
- **Redaction** — `redaction/` (**own CLAUDE.md**). **Watch sources** — `watch_sources/`
  (**own CLAUDE.md**).
- **Media in/out** — `media_download_service.py` (yt-dlp), `media_mirror_*.py`, `protected_media_providers.py`
  + `protected_media_plugins/` (+ `media_source_resolution.py`), `minio_service.py` +
  `storage_backend.py` (**see below**), `subtitle_service.py`, `formatting_service.py`.
  **Every export surface takes a required redaction config** (#85, see the gotcha below).
- **Transcript export** — `transcript_export_service.build_export_content` is the dispatcher for
  `txt json csv srt vtt md`; the formats live beside it. `transcript_export_turns.py` is the shared
  seam (`masked_segments` = the one masking path, `group_turns` = the one turn rule,
  `merge_by_timestamp`), `transcript_export_text.py` holds TXT/JSON/CSV, and
  `transcript_export_markdown.py` holds `md` (issue #564: YAML front matter written only through
  `yaml_scalar`, body escaped by `escape_line`). Every builder is a pure function of already-masked
  data (no DB, no clock) so a batch collector can reuse it (#796). The dispatcher ends in an
  explicit `csv` branch and a raise: a token added to `VALID_FORMATS` without a builder used to be
  silently served as CSV. Format token == file extension, hence `md`, not `markdown`.
- **Word and PDF transcript documents** — `transcript_document/` (**own CLAUDE.md**, #1278):
  an async job (`POST /files/{uuid}/export/document` -> `tasks/transcript_document.py`), never the
  sync `GET .../export` (which answers 400 for `docx`/`pdf`). One layout model feeds both
  renderers; fpdf2 (LGPL-3.0, unmodified) writes the PDF, python-docx the DOCX. The export
  decision (`redaction/export_policy.resolve_export_reveal`: `export_locked` beats any reveal) and
  the reveal audit (`redaction/reveal_audit.py`) are shared by every export surface, so a new
  surface calls them instead of re-deriving the rule.
- **Captions** — `captions/` is the ONE caption cue segmenter (broadcast rules, sources in
  `captions/rules.py`): the player track (`GET /files/{uuid}/captions`), the subtitle
  download, the bulk ZIP, the burned-in video and the SRT/VTT transcript export all cut cues
  there, so the player and every download agree. `captions/source.py` is the one loader:
  masked text (never written back to the ORM), canonical speaker labels. Cue times come from
  word timestamps when the words still match the text, mapped through redaction placeholders
  by a character diff; otherwise they are interpolated by character position and the WebVTT
  says so in a `NOTE`. `captions/scripts.py` holds the per-script rules (kinsoku, katakana
  words and number+counter kept whole, Thai/Lao/Khmer/Myanmar cut only at the recognizer's
  word boundaries or Thai syllable-safe points, display-column width) — dictionary-free, so a
  Chinese/Japanese break can still fall inside a word; a speaker name too long to share a line
  goes on a line of its own (`label_needs_own_line`). **Every plain-text surface masks `blur` as `label`**
  (`redaction/export_policy.plain_text_policy`): `blur`'s placeholder wraps the ORIGINAL word
  for CSS to blur, so in a file it was the original in clear (all six export formats leaked
  it before this).
- **Browser playback renditions** — `playback_rendition.py`. Every format is accepted, but
  AIFF, WMA, ALAC, AC-3, MP2 and ADPCM audio, and AVI/WMV/MPEG/TS/FLV/3GP/MPEG-4-Part-2/
  Theora video, don't play in Firefox or Chromium. Preprocessing probes the original and,
  only for those, queues `media.create_playback_rendition` (`tasks/playback_rendition.py`,
  CPU queue). That task encodes an AAC/M4A copy at `<storage_path>.playback.m4a` and
  records it in `media_file.playback_path`. For video, the copy is the audio track only.
  `resolve_playback` is the one place that picks what plays; `/stream-url` and the
  speaker-preview URL both call it. The playability tables were measured with Playwright
  in both browsers. Anything not listed gets a rendition, because an unneeded copy still
  plays and a missing one leaves the player at 00:00. ⚠️ `playback_path` is a third media
  object beside `storage_path`/`thumbnail_path`: purge, takedown tag/untag and the startup
  quarantine backfill all handle it. A new path that touches those two must handle it too.
- **Hugging Face access token** — `huggingface_token.py` is the ONLY reader of the token and
  `core/config.py` the only declaration of the env var (`tests/unit/test_hf_token_single_reader.py`
  fails on any other `HUGGINGFACE_TOKEN` access). `get_hf_token()` = token saved in Settings
  (`SystemSettings` `transcription.hf_access_token_encrypted`, `encrypt_api_key`) > environment >
  None; it opens its own short session (3 s statement timeout), caches 60 s, falls back to the
  environment on a DB error without caching that, and exports `HF_TOKEN` (an operator's own
  `HF_TOKEN` wins). Model loaders call it at load time; never from a fork/init hook (those use
  `publish_env_token_for_fork`, a dict read). Super_admin routes: `api/endpoints/admin_hf_token.py`
  (`/admin/hf-token`, write-only, fixed-sentence Test, audit entries carry the action only).
  Only the process that saved a token busts its cache at once; others see it within the TTL.
- **Ops** — backup/recovery, cleanup, migration lock+progress, task detection/filtering/recovery,
  system settings, usage, GDPR erasure (`gdpr_erasure_service.py` with `gdpr_erasure_steps.py` and `gdpr_voiceprint_erasure.py` +
  `erasure_ledger_service.py` — **see below**).
- **Superseded task rows** — `task_row_supersede.py` (#1185) is the one definition of "this active task
  row was superseded by a newer completed run of the same step for the same file": the health-check sweep,
  the `task_rows_superseded_active` gauge, `create_task_record`'s supersede-on-start and the `v448`
  migration all apply it. See `app/tasks/CLAUDE.md`.
- **Abuse takedown / quarantine** — `takedown_service.py` is the one place quarantine lives
  (a facade since #1253: `takedown_predicates` = the read side, `takedown_authority` = who may
  act, `takedown_notices` = owner notices + audit). `exclude_quarantined`/`is_hidden_for` gate
  reads, `quarantine_file`/`release_file` take a `TakedownActor` (platform or organization;
  "platform wins", see `takedown_authority`). Patch `app.services.takedown_notices.audit_logger`,
  not the service module's, in tests. Its `is_notification_suppressed` / `is_notification_suppressed_for_uuid` /
  `filter_suppressed_file_uuids` are the WS-push-side twin of that same gate (issue #908) —
  `app/utils/websocket_notify.py:send_ws_event_for_file` is the **required** call site for any
  WebSocket event naming a `MediaFile`; `send_ws_event` itself has no notion of quarantine at
  all. `tests/unit/test_ws_event_quarantine_discipline.py` is the structural gate that fails a
  new unguarded call site. …and `exclude_quarantined` is re-applied on the ASYNC export plane
  (`tasks/media_download.py`) because a dispatch-time authorization does not survive a
  takedown landing before the worker runs.
  **Notice records (issue #1254):** `quarantine_file` requires `notice=` XOR `operator_action_reason=` and writes
  the `takedown_notice_file` link BEFORE its first commit (a failure rolls the flags back), `release_file` closes
  the links in the same commit. Write side `takedown_notice_service` (never commits), reads
  `takedown_notice_queries`, owner email `takedown_notification_service` (recipients from the record only, one
  email per owner per notice, delivery log). Details: `docs/abuse-and-takedown.md`.
- **Identity / account security** — see below. `auth_config_service.py` (DB > .env > coded
  default, AES-256-GCM at rest), `account_security_service.py`,
  `idp_group_mapping_service.py`, `directory_sync_service.py`, `auth_mail_config_service.py`,
  `scim_service.py` + `scim_token_service.py` (the SCIM 2.0 write path and its credential).
  The auth *methods* themselves live in `app/auth/` (its own CLAUDE.md), not here.

## Identity services — the single-implementation rules

Five modules, each of which exists to be the **only** implementation of its rule. Adding a
second is the specific failure mode they were written to prevent.

- **`account_security_service.py`** — every credential or privilege change goes through it:
  it applies the password policy and reuse history, **revokes sessions**, and writes the audit
  event. Those three used to be applied inconsistently across `users.py` / `admin.py` /
  `password_reset.py`. Note the two revocation entry points are split by contract —
  `revoke_all_user_tokens_in_transaction` (commits nothing, propagates; the cache writes inside it are
  best-effort because the `refresh_token` rows are the record) is the only bulk entry point.
- **`idp_group_mapping_service.py`** — `resolve_grants` + `reconcile_user` turn a directory's
  claim list into in-app group membership and a role. Three callers share it: login
  (`auth/ldap_auth.py`, `auth/oidc/provisioning.py`, `auth/proxy/provisioning.py`) and the
  sweep below. `reconcile_memberships=False` / `apply_role=False` exist for the proxy path,
  where an absent groups header and an unset role header both mean "the source asserts
  nothing" — reconciling against that would strip memberships and demote admins. **`super_admin` is
  refused before anything is persisted** (`assert_grantable_role`) *and* by
  `ck_group_mapping_role_capped`; a `super_admin` is never demoted here either. Only
  directory-sourced memberships are ever removed. A privilege change revokes sessions.
- **`directory_sync_service.py`** — the periodic LDAP reconciliation/deprovisioning pass.
  Fail closed on *ambiguity*, not on error: "the directory says gone" acts, "I could not ask"
  aborts the pass. Never touches `super_admin` or `local` accounts, disables rather than
  deletes, and is bounded by `directory_sync.max_disables_per_run`. **Disabling without
  revoking would leave the refresh token rotating**, so it revokes too. Settings are six
  `SystemSettings` rows (`directory_sync.*`, defaults in `core/constants.py`) — there are no
  directory-sync `.env` vars; the super_admin API is `/admin/directory-sync`
  (`api/endpoints/directory_sync_settings.py`) and the UI is Settings -> Authentication -> Directory sync.
- **`auth_mail_config_service.py`** — which `EmailNotificationConfig` carries transactional auth
  mail (`SystemSettings` key `email.auth_config_uuid`). The read half is
  `email_service.load_auth_mail_config`. A designation naming a missing or disabled row is
  rejected at **write** time, and deleting/disabling the designated row is refused — the read
  path degrades quietly enough that a bad designation would only surface as undelivered password
  resets. `DesignationStatus.deliverable` (`resolves or env_smtp_configured`) is the ONE answer to
  "would a credential mail be handed to a transport", the rule `EmailService._send_email` applies;
  the designation route returns it (plus `verification_required`) and the admin UI branches on it.
  `tests/api/test_auth_mail_deliverable.py` drives the real sender and this description from the
  same rows so they cannot drift. It means configured, not working. The route is super_admin
  only; exposing it to plain admins (so the invite form can warn them) was deliberately NOT done
  and needs an authorization review first.
- **`scim_service.py`** — every write `/scim/v2` performs. Deactivation revokes sessions,
  `super_admin` is untouchable, roles are never written at all, and `DELETE /Users/{id}` is a
  soft-disable. `scim_token_service.py` owns the credential: SHA-256 at rest, revoked and
  expiry checked on the same read that finds the row, revocation one-way.

`README.md` in this directory is the older long-form tour; it drifts — trust the code.

## GDPR Art. 17 erasure — the three rules that are not obvious (issue #442)

The three rules (and the receipt/measurement contract) are in `backend/app/services/erasure_receipt/CLAUDE.md`. Read it before touching any `*erasure*` module.
## Speakers: removal, unassigning, and where a label comes from

Three load-bearing rules live in `backend/app/services/speaker_matching/CLAUDE.md`: removing a voice goes through `speaker_cleanup.py` only (issue #1214), unassigning removes the section from that person (#1233), and **a speaker's label comes from a human or a voiceprint, never from an LLM** (owner decision 2026-10-12). Read it before touching any speaker delete/move/label code.
## Speaker embeddings: two backends, one model (issue #571)

Detail in `backend/app/services/speaker_matching/CLAUDE.md`.
## Object storage: `storage_backend.py` + `minio_service.py`

Backend selection, presign identity, absence/recovery rules: `backend/app/services/notes/storage.md`.
## LLM features (optional)

Provider handling, per-model reasoning capability (measured, `llm_reasoning.py`, #64) and the measured context window (`llm_context_window.py`, #533/#833): `backend/app/services/notes/llm-features.md`. Both capabilities are MEASURED per model, never assumed from a name.

**Token limits (#790, D13) are enforced at ONE seam: `LLMService.chat_completion` and
`chat_completion_stream`** (`llm_budget.py`). Every feature (chat and its planning calls,
summaries, topics, speaker ID, the LLM redaction detector) reaches the provider through those two
methods, so never add a per-feature check; a new LLM caller is covered by going through them. The
limit is TOKENS per user per UTC day and per instance per UTC month (`SystemSettings`
`llm.budget.*`, 0 = unlimited, admin UI Settings -> AI usage, `/admin/llm-budget`), so an unpriced
provider cannot slip past it. Accounting is a reserve-then-settle on `llm_token_budget` with one
conditional `UPDATE` per counter (race-safe under READ COMMITTED). A refusal raises
`LLMBudgetExceededError` (`code` `llm_budget_exceeded`, or `llm_budget_unavailable` when a set
limit cannot be checked: it fails closed); the stream yields it as an `error` event with that
code. `llm_usage_run` re-raises a refusal at exit even when the feature swallowed the call's
error, and each task records the code via `llm_budget.refusal_of`, never as success. Whose daily
limit applies: `LLMService.budget_user_id` (set by the per-user factories), else the open run's
file owner. Only the connection test (`validate_connection`) and the admin reasoning probe
(`chat_completion_raw`) are not limited. Pricing (`chat/pricing.py`) is free only for an event
recorded `local: true`, never by provider name.
## User transcription settings

Per-user prefs (Settings → Transcription and Settings → Speaker Identification; one endpoint, saved per tab group) are `UserSetting` key/value rows shaped by
`schemas/transcription_settings.py` and served from `api/endpoints/user_settings.py`
(`GET/PUT /user-settings/transcription`): source language + translate-to-English, LLM output
language, speaker behavior (`always_prompt` | `use_defaults` | `use_custom`), min/max speakers,
garbage-segment cleanup + threshold, VAD tuning; recording/audio-extraction live on sibling
routes. **Per-file overrides win, for these only**: speaker counts (`resolve_speaker_range` in
`tasks/transcription/user_settings.py` is the one precedence — per file, then the user's saved
range, then env), the Whisper model (the deployment's model or tiny/base; anything else is a
422), and "skip diarization". They are stored on the `media_file.requested_*` columns when a
file is dispatched, and a retry/recovery replays them (`reuse_requested_options`,
`tasks/transcription/requested_options.py`). **Language is NOT per file**:
`dispatch_transcription_pipeline` still accepts `source_language`/`translate_to_english`, but no
endpoint sets them, so language and translation always come from the user's prefs. Adding them
to the upload/reprocess requests is a separate decision.

## Pipeline composition: `pipeline_plan.py` is the one decision point (#1224)

Transcription and speaker detection are chosen independently, each local or cloud.
`pipeline_plan.resolve_pipeline_plan(PlanInputs) -> PipelinePlan` is a **pure** function
(constants only, no DB) that decides, for every combination of transcription engine
(`local_gpu` / `local_cpu` / `cloud`), stored speaker-detection source, deployment (full /
lite) and the model's timestamp granularity: which detector runs (`asr` / `local` /
`pyannote_ai` / `asr_config` / `off`), how (`inline_asr`, `inline_local`, `parallel_cloud`,
`post_stage_local`, `post_stage_cloud`), where, on which queue, how turns are merged
(`word` / `segment`), and the voiceprint step (always this server: `inline_native` after the
local engine, otherwise `embedding_stage` on `cpu`). Problems are `PlanIssue`s with a stable
`code` (also the i18n suffix `settings.pipeline.issue.<code>`) and a fixed sentence in
`ISSUE_MESSAGES`; only `severity == "error"` blocks. `pipeline_plan_inputs.load_plan_inputs`
gathers the inputs (active ASR via `ASRProviderFactory.resolve_active_config`, the #1109 lock,
the pyannote.ai key, the catalog capabilities); `plan_for_user` composes the two.

Owner decisions it encodes (do not re-open): lite has no local transcription and no local
speaker detection; Automatic with a text-only model detects speakers on this server (full only);
**no forced alignment / wav2vec2** — a model with `timing == "none"` is refused for every speaker
detector (`asr_no_timing_for_speakers`); voiceprints are never taken from a vendor. Every cell is
pinned by `tests/unit/test_pipeline_plan.py` (full and lite tables). On lite a local
detector is never planned to run: the cell keeps `engine: local` and its blocking issue but
plans `execution: none` (no queue, no voiceprint step), and "this server" is refused even when
transcription is local too (#1228). Consumers: the settings
PUT (409 on a NEW selection with a diarization-step error), the ASR-side switches
(`services/asr_switch_plan`: diarization-step errors the switch introduces, and every
transcription-step error), `GET /user-settings/pipeline`, and every pipeline
stage (#1225: dispatch resolves it once and carries it in the chain context; what each cell
then does is pinned by `tests/unit/test_pipeline_execution_matrix.py`).

## Per-file processing provenance (#1229): one writer, one reader

`media_file.processing_provenance` (JSONB, `v437`, `{"v": 1, "steps": {<step>: {...}}}`, shape
in `schemas/processing_provenance.py`) records, per step, which provider/model completed it and
where it ran. Steps: `transcription`, `diarization`, `voiceprints`, `summary`, `topics`,
`speaker_names`.

- **Write ONLY through `file_provenance.record_stage(db, file_id, stage, **facts)`.** It is one
  `UPDATE ... jsonb_set` statement (never read-modify-write: the LLM tasks finish concurrently
  for one file; `tests/unit/test_file_provenance_record.py` holds a row lock to prove it) inside
  a savepoint, and it logs and swallows a DB error so provenance can never fail the stage write
  it rides in. Unknown fact names raise `TypeError` (`base_url`, `api_key` are refused by
  construction); a URL-shaped value is dropped. `new_run=True` (the transcript save) starts a
  fresh document, so an earlier run's voiceprints/summary never describe a new transcript.
- The `*_facts` helpers (`transcription_facts`, `diarization_facts`, `voiceprint_facts`,
  `llm_facts`, `inline_speakers_by`) turn what a stage knows into canonical facts so call sites
  stay one line. `llm_facts` calls `llm_guard.is_local_provider`, which can resolve a hostname:
  call it in the no-session phase, never inside a write session.
- Speaker-detection ids are canonical: `native` / `pyannote` = the local engines (server),
  `pyannote.ai` = the cloud service, a transcription vendor id = that vendor's own speakers.
  A legacy `pyannote` cannot be told apart from pyannote.ai, so the reader gives it no location.
- **Read ONLY through `file_provenance_view.build_processing_steps(media_file)`**, served as
  `processing_steps` on `MediaFileDetail` and `GET /my-files/{uuid}/status`. Display names are
  proper nouns, pre-formatted here. `NULL` = processed before recording began: rows are
  composed from the older columns with `recorded="legacy"` and anything they cannot say is
  `not_recorded`; a document with a `transcription` entry means a missing step is `not_run`.
- The scalar columns (`asr_provider`, `diarization_provider`, ...) stay the queryable index and
  are still written where they were; `record_diarization` updates them for a post stage.

## Media URL ingestion (yt-dlp)

`media_download_service.py`. 1800+ platforms, no extra config — yt-dlp and a Deno runtime ship
in the backend image (`js_runtimes` at `/usr/local/bin/deno`, required since yt-dlp 2025.11 for
YouTube PO tokens; `YOUTUBE_EXTRACTOR_ARGS` rotates player clients).

- Limits: **15 GB** (`max_filesize`, matches the upload limit) and **4 h** (`duration > 14400`).
- yt-dlp failures return a fixed literal and log the real error; the raw message can carry
  cookie/config paths or the URL (#859). Vimeo / Instagram / Facebook / LinkedIn / Patreon usually need auth.

## Exports and redaction — three rules that are not obvious (issue #85)

The three rules are in `backend/app/services/transcript_document/CLAUDE.md` ("Exports and redaction").
## Gotchas

- Settings that look like env vars are frequently **DB-backed** (`SystemSettings` /
  `UserSetting`) with coded defaults in `core/constants.py`. Check before adding an env var.
- GPU worker, CPU worker, and `celery-redaction` load different models — importing a
  model-loading service into a task on the wrong queue pulls weights onto the wrong device.
- Docs: `docs-site/docs/features/llm-integration.md`,
  `docs-site/docs/features/transcription.md`,
  `docs-site/docs/configuration/neural-search-setup.md`.
