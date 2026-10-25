# app/services — business-logic layer

## Purpose

Everything between the API/Celery entry points and the data layer. ~110 modules; endpoints and
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
  `file_cleanup_service.py` and `tasks/opensearch_integrity_task.py`. Since #963 a summary is
  ALSO indexed as a derived, rebuildable `doc_type: "summary"` plane inside `transcript_chunks`
  — never a second store of record; see `search/CLAUDE.md`'s "Index v6" section.)
- **Speakers** — `speaker_*_service.py`, `profile_embedding_service.py`,
  `smart_speaker_suggestion_service.py`, `embedding_mode_service.py`,
  `metadata_speaker_extractor.py`. **Any code that deletes a speaker or moves one out of a
  cluster or profile goes through `speaker_cleanup.py`** — see "Removing a voice" below.
- **Collection visibility** — `collection_visibility.py` is the one place a surface asks
  which collections a caller may see named on a file or filter by: `visible_collection_ids`
  (the predicate #1275's restricted collections will replace), `collections_on_files`
  (batched, constant queries per page; the file page's `get_file_collections` and the search
  card's `collections` both use it) and `resolve_collection_filter` (a hidden collection
  filters to nothing, never 403/404). Don't add a raw `Collection` join beside it.
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
- **Media in/out** — `media_download_service.py` (yt-dlp), `media_mirror_*.py`,
  `protected_media_providers.py` + `protected_media_plugins/`, `minio_service.py` +
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
  system settings, usage, GDPR erasure (`gdpr_erasure_service.py` +
  `erasure_ledger_service.py` — **see below**).
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
  `revoke_all_user_tokens_in_transaction` (commits nothing, propagates) vs
  `revoke_all_user_tokens` (best-effort, commits, unsafe mid-transaction).
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
  directory-sync `.env` vars, no endpoint, and no admin panel.
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

`gdpr_erasure_service.py` destroys the data; `erasure_ledger_service.py` records that it
was asked for; `tasks/erasure_reconciliation.py` finishes what could not finish. All
three are needed, and dropping any one reproduces a defect that shipped.

- **The ledger must not contain the personal data it records the destruction of.** "We
  erased alice@example.com" *containing* the address is a copy of the thing that was
  supposed to be destroyed, in a table designed to outlive it. So `erasure_ledger` has
  **no free-text column at all** — every textual column is a short enum with a CHECK —
  and the one JSONB column is nailed shut by `ck_erasure_ledger_counters_numeric`
  (`jsonb_path_exists`, IMMUTABLE, so it is legal in a CHECK where a subquery is not).
  There is deliberately **no email hash** either: a hash of a value from a guessable
  space is pseudonymous personal data (Recital 26), not anonymous. Subjects are named by
  `id` + `uuid` only, which are meaningless once the row they point at is gone — and
  meaningful again exactly when a restore brings it back, which is the property the
  resurrection check relies on. `record_outcome` drops `summary["errors"]` entirely: its
  entries carry file UUIDs and driver messages that quote filenames and storage paths.
- **`subject_user_id` / `subject_organization_id` are NOT foreign keys, on purpose.**
  They name the rows the table records the destruction of. A real FK would either block
  the delete (`NO ACTION`) or null the column (`SET NULL`) — and nulling it destroys the
  only key the reconciliation sweep has. Referential integrity is the property these
  columns must not have. `actor_user_id` **is** an FK, `ON DELETE SET NULL` like every
  actor FK since v387; `tests/unit/test_user_deletion_fk_coverage.py` records the
  disposition.
- **The ledger cannot only be a table.** It lives in Postgres, so restoring a dump taken
  *before* an erasure destroys the record of the erasure along with the erasure — it
  would have to survive its own failure mode. Every entry is therefore also appended to
  a line-delimited journal under `DATA_DIR/gdpr/`, outside the dump;
  `restore_from_journal` re-opens anything the database has lost, keeping the ORIGINAL
  `requested_at`/`sla_due_at` so a restore cannot buy another month of Art. 12(3) time.
  Its limit is honest: one file on one volume. Off-host replication is deployment
  config; the audit stream is the second copy that already leaves the host.
  `ERASURE_JOURNAL_BACKEND=object_storage` writes one object per entry to the media
  bucket instead, for containers with no durable writable volume (read-only root fs) —
  never "fix" an unwritable `DATA_DIR` with an ephemeral mount; the journal would die
  with the pod.

Two judgement calls worth not re-litigating blindly:

- **A legal hold retains the ACCOUNT, not just the file, and that is forced by the
  schema** — `media_file.user_id` is a plain `NO ACTION` FK, so `DELETE FROM "user"`
  raises while a held file exists. Art. 17(3)(e) only justifies retaining the *file*, so
  the account's survival is a side effect, made temporary by the ledger entry and the
  sweep. Making it genuinely file-granular means anonymising the account instead —
  a design change, not a patch.
- **`org_member` entries are never auto-re-erased after a restore.** That scope never
  deletes the `user` row, so "is the subject present" is always true; the only other
  signal ("does the member have org rows again?") is indistinguishable from the member
  legitimately uploading to that tenant the next day, and acting on it would destroy
  data nobody asked to erase. They are counted as `org_member_manual_review` instead.

## Removing a voice: `speaker_cleanup.py` is the only implementation (issue #1214)

A speaker's voice is copied into more than its own voiceprint document: every cluster it
belongs to stores a `cluster_<uuid>` centroid (a weighted mean of the members' voiceprints,
profile-linked members counting double), its profile stores a `profile_<uuid>` embedding (a
mean of the members'), and the transcript index planes carry its label. Deleting the speaker
row, or moving it out of a cluster or profile, changes none of those by itself — and the
`ON DELETE CASCADE` on `speaker_cluster_member` erases the only record of which centroids
averaged it in. #1211 fixed this for the file purge alone; every per-speaker path kept the
removed voice inside some aggregate.

Every path now does the same two calls around its own write:

```python
impact = capture_speaker_impact(db, [speaker_id], removed=True, reindex=True)  # BEFORE
db.delete(speaker)
db.commit()
raise_if_incomplete(reconcile_speaker_impact(db, impact))  # AFTER commit
```

Callers: `speakers.py` (delete, merge, verify accept/reject/create, profile moves in the
update task), `transcript_segments.py` (orphan cleanup), `speaker_update.py` (auto-profile
and retroactive matching), `speaker_profiles.py` (profile delete), `speaker_clusters.py`
(cluster delete), `speaker_clustering_service` (unassign, split, merge, promote, batch
assign, upload-time clustering, re-cluster), `speaker_matching_service` (profile
propagation), `speaker_embedding_task` (segment-reassign voiceprint drift),
`files/reprocess.py` (transcription and rediarize clears), `task_utils.reset_file_for_retry`,
`speaker_merge_task` (the target half) and `file_cleanup_service.purge_media_file`.

The same module is also the ONE implementation of **assignment** bookkeeping (#1233): a path
that ADDS a speaker to a cluster or profile names what it touched in a `SpeakerImpact` and
hands it to `reconcile_speaker_impact` after its commit, exactly like a removal — so
membership rows, `Speaker.cluster_id`, `member_count`, the centroid, the profile embedding
and the speaker's own document's `profile_id`/`display_name` (`relabel_speaker_ids`) agree.

The rules it applies are the product's existing ones, written once:

| After the change | Result |
|---|---|
| The removed speaker's own voiceprint | erased from every speaker index, verified by a survivor count |
| A cluster with 0 members left | row deleted and centroid erased, **promoted or not** (`batch_recluster` rebuilds promoted clusters from profiles; an empty one was listed with nobody in it) |
| A cluster with 1 member | kept, centroid = that member (upload-time singletons are how matching works; `list_clusters` hides them, Re-cluster All only rebuilds groups of 2+) |
| Any other cluster | `member_count` and centroid recomputed, `_update_cluster_centroid(strict=True)` |
| A profile that lost or gained a member | embedding recomputed, `update_profile_embedding(strict=True)` |
| A centroid or profile embedding that cannot be rebuilt | **erased** (absent is rebuilt later; stale is a copy of the removed voice) |
| A file whose segments lost their speaker | `dispatch_transcript_reindex` — chunk, digest and summary rosters rebuilt from Postgres |

- **Failures are returned, never swallowed.** `reconcile_speaker_impact` never raises and
  logs every residual at ERROR; request paths call `raise_if_incomplete`, which raises
  `SpeakerCleanupIncompleteError` (a `SearchIndexError`, so `main.py` answers **503** with the
  fixed `CLEANUP_INCOMPLETE_MESSAGE` — no driver text). Background and bulk paths (merge
  task, update task, retry, reprocess) keep going and report `status: partial` or the log.
  The change itself has committed either way; the 503 says the cleanup did not finish.
- **Capture BEFORE the write, reconcile AFTER the commit.** After the delete, Postgres can no
  longer say which clusters held the speaker; before the commit, a rollback would leave the
  aggregates rebuilt for a change that never happened.
- **A merge is split in two on purpose.** The endpoint rebuilds the SOURCE's clusters and
  profile synchronously but does not erase its voiceprint — `process_speaker_merge_background`
  reads it to average into the target, then `_reconcile_speakers_after_merge` rebuilds the
  target's side and verifies the source document is gone.
- **Deleting a profile restores labels.** A linked speaker carries the profile's name as its
  display name (`speaker_profile_rename`'s invariant), so members whose display name *is*
  the profile's name get it cleared and the chunk plane is told through
  `SpeakerRenameTracker`; a name the user set to something else survives. A cluster labelled
  with the profile's name loses the label.
- **A reindex that could not be queued is a residual** (#1233). `_reindex_files` calls
  `search/reindex_dispatch.queue_transcript_reindex`, which returns `QUEUED`,
  `ALREADY_PENDING` (the debounce window already holds the reindex this change needs) or
  `FAILED`; only `FAILED` becomes a `transcript_index` residual. `dispatch_transcript_reindex`
  is the bool view of the same call for callers that only ask "queued now?". A failed
  publish also releases the debounce claim it took — keeping it told every later mutation
  of that file, for the whole window, that a reindex nobody queued was pending.

### Unassigning: the section is no longer that person (issue #1233)

The owner's rule, implemented once, in `speaker_cleanup.detach_from_cluster`:

| Unassigning a speaker from a cluster… | |
|---|---|
| membership | both representations removed (`speaker_cluster_member` row and `Speaker.cluster_id`) |
| the cluster's promoted profile | unlinked by `unlink_from_profile` — the profile-delete rule: `profile_id`/`verified` go, a display name or suggestion that IS the profile's name goes, a name the user typed stays |
| blacklist (`blacklist=True`, the default) | cannot-link against **every remaining member**, both directions, **and** a profile blacklist row — both halves, promoted or not |
| the cluster and profile | **remain** for everyone else; centroid and embedding rebuilt from them; an emptied cluster follows the empty-cluster rule above |

`split_cluster` is the same unassignment (the moved group is someone other than those who
stay), then the group forms a new unpromoted cluster. `blacklist=False` is the UI's explicit
opt-out: it still unlinks, it records nothing.

**Every assignment path honours the blacklist** through `speaker_blocked_from_cluster` /
`speaker_blocked_from_profile` (blocked = blacklisted from the profile, or cannot-link with
any member / any speaker linked to the profile) or, where the path assigns by NAME,
`blocked_profile_names`:

| Path | Before #1233 | Now |
|---|---|---|
| `find_or_create_cluster` (upload) | checked a promoted cluster's profile blacklist; cannot-link one direction only; linked the profile without rebuilding its embedding; dropped stale memberships without rebuilding those clusters | helper check (both directions); joins via `_join` → `join_cluster_identity`; `_settle_assignments` rebuilds the joined cluster, every cluster the speaker was dropped from, and every profile it entered or left |
| Re-cluster Phase 1 (profiles) | regrouped any linked speaker, including one blacklisted from that profile | a link the user already unassigned (pre-#1233 data) is taken back by `unlink_from_profile` first |
| Re-cluster Phase 2 (AHC) | cannot-link eviction | unchanged (already honoured) |
| Re-cluster Phase 2.5 (singleton → profile cluster) | profile/cannot-link check; joined WITHOUT the profile (no label, centroid not rebuilt) | helper check; joining links the profile at the auto-link bar `max(threshold, 0.75)`; centroid + embedding settled after commit |
| retroactive matching (`speaker_update.trigger_retroactive_matching`) | none | blocked candidates are skipped entirely — neither auto-applied nor suggested |
| batch verify `assign` / `accept` / `name` | none | a blocked speaker is refused with a per-speaker error and listed in `blocked_speaker_uuids` (a bulk action is where a correction gets swept back in unnoticed); `name` was added in the review pass |
| `promote_cluster_to_profile` | — | nothing to block: an unassigned speaker is no longer a member |
| `merge_clusters` | moved every member; target's profile not propagated | a blocked member stays in the source (which survives with it) and is listed in the route's `blocked_speaker_uuids`; moved members take a promoted target's profile |
| `_propagate_profile_assignment` | none; profile embedding not rebuilt | blocked candidates skipped, and (review pass) so is one named anything but the profile's name; profile + their clusters rebuilt |
| `_handle_speaker_match` (re-match of an existing speaker) | none | blocked identity is neither suggested nor linked |
| segment-reassign voiceprint update | rebuilt profiles only (non-strict); clusters drifted | `_rebuild_affected_aggregates` → `reconcile_speaker_impact` over the target's and source's clusters and profiles |
| speaker merge | source's rows died with it (CASCADE) | `carry_blacklist` copies them to the target first (except the profile the target is already linked to) |
| `app/scripts/batch_speaker_matching.py` | none | skips a suggestion of a blocked name |
| LLM speaker ID (`speaker_identification_task._store_speaker_predictions`) | none | skips a blocked name (and a speaker that already carries a voiceprint suggestion). It never moves the canonical label: see "A speaker's label comes from a human or a voiceprint" below |

**The explicit override.** Naming ONE speaker as a profile — typing the name
(`auto_create_or_assign_profile`), `assign-profile`, verify `accept`, or a `batch-verify`
of exactly one speaker with `override_unassignment: true` (the Inbox's per-speaker
accept/assign, which is where an unassigned speaker lands and where a wrong unassignment
is undone) — calls `lift_profile_blacklist`: it drops the speaker's blacklist row for that
profile and its cannot-links with the profile's speakers. Only those single-speaker
explicit actions lift it; `override_unassignment` with more than one speaker is a
`ValueError` (422 at the route), so a batch can never carry a lift.

**Review pass (#1233, 2026-10-12): rules the first sweep did not cover.**

| Rule | Where |
|---|---|
| A bulk action REPORTS what a correction skipped: `batch_verify_speakers` and `merge_clusters` return `blocked_speaker_uuids`, and the SPA shows it (Inbox error toast, merge warning toast `speakers.merge.someLeftBehind`) instead of a success | `speaker_clustering_service`, `routes/speakers/+page.svelte` |
| Batch `name` honours a correction like `accept` (it used to bypass it) | `batch_verify_speakers` |
| **An automatic path never overwrites an identity a speaker already has**: `has_other_identity` = linked to another profile, or named (typed, or an accepted suggestion) something other than the profile. `find_or_create_cluster`, Re-cluster Phase 2.5 and profile propagation skip such a speaker; `merge_clusters` leaves it in the source and reports it (except members of the SOURCE's own profile — merging two named clusters says the names are one person) | `speaker_cleanup.has_other_identity` / `identity_conflicts_with_cluster` |
| **Decision 5: a speaker linked to another profile is no longer in the old profile's cluster.** `reconcile_speaker_impact` releases, from every promoted cluster it refreshes, members linked to a different profile — so renaming, assigning, accepting, batch-assigning or retroactively matching a member away takes it out (no blacklist: the new link already says who it is) | `speaker_cleanup._release_members_of_other_profiles` |
| **Rejecting an identification is an unassignment**: `verify?action=reject` leaves the linked profile's clusters through `detach_from_cluster` (blacklist both halves) and records a profile blacklist (`rejected_by_user`) for the linked profile and the profile its suggestion named | `speakers._reject_speaker_suggestion` |
| **A moved segment leaves the old speaker's voiceprint**: the reassignment task removes the segment's embedding from the source by the exact inverse of the weighted add the target gets, and both rewritten documents keep the file's tenant (they were rewritten without `organization_id`, i.e. as personal) | `speaker_embedding_task._remove_segment_from_voiceprint` |
| **A correction survives a re-run of the file** (rediarize, re-transcribe, retry), which deletes the speakers and, by CASCADE, their corrections: stashed by the corrected speaker's time spans before the delete, re-applied when the re-run saves its segments to every new speaker whose speech is ≥ `INHERIT_SHARE` (0.5) inside them | `speaker_correction_carryover` (table `v438`) |
| Suggestions never offer a refused identity (speaker list, profile suggestions endpoint), and the voice-match suggestion of `_handle_no_speaker_match` (a >=0.75 voiceprint one moves the canonical label) skips a refused name or cannot-link partner | `SmartSpeakerSuggestionService.without_refused`, `SpeakerMatchingService._refusals` |

Deliberately unchanged: deleting a cluster records nothing (it discards a grouping; it
is not a statement that the members differ), and `blacklist: false` on unassign records
nothing (the UI's "Prevent from rejoining" opt-out).

**Autoflush is off.** A caller that sets `profile_id` and rebuilds in the same transaction
must flush first (`speaker_update._update_profile_embedding` does): the rebuild otherwise
reads the membership from before the link, and the new voice never reaches the embedding.

## A speaker's label comes from a human or a voiceprint, never from an LLM (owner decision 2026-10-12)

The canonical label (`utils/speaker_labels.canonical_speaker_label`: what the chunk index,
digests, facts, chat speaker resolution, exports and the speaker API's `resolved_display_name`
show as the speaker's name) is **(a)** `display_name` — a name a human typed or verified — or
**(b)** a `suggested_name` whose `suggestion_source` is in `EMBEDDING_SUGGESTION_SOURCES`
(`voice_match`, `profile_match`: written only by the voiceprint matchers) at
`confidence >= 0.75`, else the diarizer label. An LLM suggestion (`llm_analysis`) is shown in the
suggestion UIs with its confidence and nothing more; accepting it writes `display_name`.

- **Allow-list on provenance, fail closed.** A source that is not on the list — `llm_analysis`,
  `metadata_hint`, `user_skipped`, a new producer, or **no source at all** (legacy rows) — never
  reaches the label. `test_speaker_label_provenance.py::test_every_source_the_app_writes_is_classified`
  scans `app/` for every `suggestion_source` literal and fails on an unclassified one.
- **Pass the source at every call site**, or use `canonical_speaker_label_for_row`. A raw-column
  query (`reindex_task`, `rename_propagation_task`, the chat roster) must select
  `Speaker.suggestion_source` too; omitting it silently reads as "unknown source".
- **The chat roster only finds a speaker through `suggested_name` when a voiceprint wrote it**
  (`speaker_resolver.build_candidate_roster`): a "who said X" question must not resolve a speaker
  through a guess.
- **The LLM task will not overwrite a voiceprint suggestion** — that would replace the evidence
  the label rests on with a guess.
- **Existing indexes**: `services/speaker_label_backfill.py`, an arm of the existing
  `search_index_maintenance` task, re-indexes (per-file, debounced) each completed file with a
  speaker whose old label was an LLM suggestion, refreshes the persisted
  `speaker.resolved_display_name`, and regenerates `file_facts`. Resumable by an id cursor in
  `SystemSettings`, 100 files per tick, finished by the `speaker_label_provenance_backfill_complete`
  flag. No manual reindex is needed.
- **Legacy rows with no source** (written before `v091` added the column, when the LLM task and
  the voiceprint matchers wrote identical `suggested_name`/`confidence` pairs) are stamped by
  migration `v439` only where Postgres proves a voiceprint: `profile_match` when the speaker is
  linked to a profile whose name IS the suggestion (no LLM path has ever set `profile_id`), and
  `voice_match` when a `speaker_match` row pairs it with a same-owner speaker whose
  `display_name` IS the suggestion at exactly the speaker's own `confidence`. Everything else
  stays unsourced and falls to the diarizer label (fail closed). The speaker API reports such a
  row's `suggestion_source` as `null`; it used to default it to `voice_match`.

## Speaker embeddings: two backends, one model (issue #571)

`speaker_embedding_service.py` no longer always loads a model. For **v4** (256-d) it prefers
`native_embedding_client.py` — the diar-native sidecar's `/embed_window` — and loads nothing
in-process; the sidecar runs the **same** `pyannote/wespeaker-voxceleb-resnet34-LM` weights,
re-exported to ONNX. That is measured, not assumed: over 134 AMI ground-truth single-speaker
windows (6 meetings, 21 speakers) the two paths agree at cosine **0.9999997** (min 0.9999993)
on identical audio, against a same-speaker mean of 0.846 and a different-speaker mean of 0.094.
`tests/integration/test_native_embedding_equivalence.py` re-asserts it against both real
implementations.

Backend selection lives in one predicate, `SpeakerEmbeddingService._native_backend_usable()`:

| Condition | Backend |
|---|---|
| v4, sidecar healthy, `USE_NATIVE_SPEAKER_EMBEDDINGS` unset/true, default model | `native` |
| **v3** (`pyannote/embedding`, 512-d) | `pyannote` — always |
| `USE_NATIVE_SPEAKER_EMBEDDINGS=false` | `pyannote` |
| sidecar unreachable at construction, or failing mid-run | `pyannote` |
| an explicitly pinned non-default `model_name` | `pyannote` |

- **v3 is excluded on purpose, and it is the whole reason a switch exists.** `pyannote/embedding`
  is a *different network*, not a different export — 512-d, and no linear map relates it to the
  256-d WeSpeaker space. Routing v3 at the sidecar would write 256-d vectors into a 512-d index.
  A 512-d install leaves PyAnnote by the migration that already exists
  (`tasks/embedding_migration_v4.py`, `docs-site/docs/configuration/embedding-migration.md`), and
  because those tasks ask for a service **by mode**, they inherit the native backend for their v4
  half for free — the same one switch governs a live transcription and a migration batch.
- **`USE_NATIVE_SPEAKER_EMBEDDINGS` stays an escape hatch, not a dead flag.** It keeps its
  existing meaning on the diarizer-centroid path (`tasks/transcription/embeddings.py`) and now
  also forces this service in-process, so one variable fully reverts to PyAnnote.
- **`model_name` names the WEIGHTS, `backend` names how they run.** `get_cached_embedding_service`
  keys its warm cache on `(mode, model_name)`; a sidecar-specific name there would miss on every
  lookup and rebuild the service per call.
- **Fallback is sticky for the instance's lifetime.** A sidecar loss mid-run loads the in-process
  model and flips `backend`, matching `NativeSpeakerDiarizer`'s mid-job fallback. It returns to
  native only via `clear_embedding_cache()`.

> ⚠️ **The sidecar's window length is load-bearing, and getting it wrong fails SILENTLY.**
> `/embed_window` embeds a fixed **160,000-sample (10 s)** window under an *all-ones* 589-frame
> mask (`diar-core`'s `MaskedEmbeddingInput`). A shorter clip is **zero-padded to 10 s and the
> padding is pooled at full weight** — you get back a plausible unit-norm 256-vector, nothing
> raises, nothing logs. Measured cosine against the in-process model by clip length:
>
> | 0.8 s | 1 s | 2 s | 3 s | 5 s | 8 s | 10 s |
> |---|---|---|---|---|---|---|
> | +0.012 | +0.028 | +0.007 | +0.081 | +0.239 | +0.901 | **+1.000** |
>
> At 3 s the sidecar's own speaker discrimination collapses to **EER 39.6%** where the in-process
> model scores 0.1% on the very same windows. `NativeSpeakerDiarizer.embed_window` posted the raw
> slice until #571, so the acoustic boundary re-check (default OFF) was scoring noise.
> **Never post a clip to `/embed_window` directly** — go through
> `native_embedding_client.fit_to_window`, which repeat-fills short clips (measured 0.989 vs the
> in-process model) and tiles long ones (0.9977–0.9996 at 20–60 s), capped at
> `NATIVE_EMBEDDING_MAX_TILES` because nothing upstream bounds segment duration.

## Object storage: `storage_backend.py` + `minio_service.py`

`minio_service` is the API (upload/download/presign/lifecycle, ~60 call sites plus the
`minio_client` singleton). `storage_backend` is the *policy* layer underneath it and owns
every difference between the two backends (issue #284 A1.11/A1.12):

| | `STORAGE_BACKEND=minio` (default) | `STORAGE_BACKEND=s3` |
|---|---|---|
| Endpoint | `MINIO_HOST:MINIO_PORT` | `S3_ENDPOINT_URL`, else `s3.<S3_REGION>.amazonaws.com` |
| Credentials | static `MINIO_ROOT_*` | AWS chain (env → IRSA → ECS → IMDS) unless `S3_USE_IAM_ROLE=false` |
| Addressing | path-style | virtual-host (minio-py switches on AWS hostnames) |
| Presigned host | rewritten to `STORAGE_PUBLIC_URL`/`MINIO_PUBLIC_URL`, else `/s3` | **not rewritten** |
| Single-PUT ceiling | 5 TiB | 5 GiB (`supports_single_put`) |
| Abandoned-multipart expiry | MinIO's own scan (24 h) | lifecycle rule (`ensure_abort_incomplete_lifecycle`) |
| Bucket CORS | implicit | opt-in `S3_CONFIGURE_BUCKET_CORS` (boto3 — minio-py has no CORS API) |

- **One SDK for both.** The client is always `minio.Minio`; minio-py is a generic S3 SDK,
  so switching backends changes construction, not the 60 call sites. boto3 appears in
  exactly one place — `ensure_bucket_cors`.
- **`clamp_presigned_expiry` gates every presigned URL** (`PRESIGNED_URL_MAX_SECONDS`,
  6 h). A presigned URL cannot outlive the credentials that signed it, and IAM-role STS
  sessions expire well inside 24 h, so a longer URL just starts 403-ing. `get_file_url`'s
  old 24 h default arg is gone.
- **Large uploads go browser-side multipart** (`multipart_upload.py`, issue #327).
  `build_upload_plan` is the single decision point `/files/prepare` calls: multipart at or
  above `multipart_threshold_bytes()` (`MULTIPART_THRESHOLD_MB`, 512 MB, clamped to the
  single-PUT ceiling so >5 GiB on `s3` is *always* multipart), one presigned PUT below it,
  `None` → the client falls back to `POST /files`. Part URLs are signed **8 at a time**
  (`PART_URL_BATCH`), not once for the whole object: they take the same
  `clamp_presigned_expiry` as everything else and a 15 GB upload can outlive a 6 h clamp.
  `/files/multipart/parts` signs the next batch and, on resume, lists the parts storage
  already holds. `/files/complete` assembles them (client ETags, else read back).
- **Abandoned multipart uploads must be aborted** — S3 and MinIO both bill for the parts,
  and they never appear in an object listing. `cancel_upload` (`DELETE /files/{uuid}`) calls
  `abort_uploads_for_object`, which finds the uploads by key because the `upload_id` is
  client state. `ensure_abort_incomplete_lifecycle` adds the storage-side backstop and is
  **native-S3 only**: MinIO's ILM rejects an `AbortIncompleteMultipartUpload` rule
  (`InvalidArgument`; it silently drops the action from a mixed rule) and does not need one —
  it purges stale uploads itself via `api.stale_uploads_expiry` (24 h).
- **minio-py's multipart primitives are underscore-prefixed** (`_create_multipart_upload`,
  `_complete_multipart_upload`, `_abort_multipart_upload`, `_list_parts`,
  `_list_multipart_uploads`). Driving them keeps the one-SDK rule above; boto3 would mean a
  second client with its own copy of the endpoint/credential policy.
  `tests/unit/test_multipart_upload.py` asserts they still exist so an SDK bump fails in CI.
  Part *signing* is the public `get_presigned_url(..., extra_query_params=...)`.
- Don't reintroduce a second host-rewrite. `MinIOService.get_presigned_url` used to
  hardcode `http://minio:9000` → `localhost:5178`/`EXTERNAL_MINIO_URL`; it now shares
  `rewrite_public_host` like everything else.
- **Two presigning clients, not one (issue #907).** `minio_client` (root) signs everything
  except browser-facing GETs; `storage_presign_identity.presign_client()` — a dedicated,
  least-privilege MinIO service-account identity — signs the three that reach a browser:
  `get_file_url`, `get_presigned_download_url`, `MinIOService.get_presigned_url`. Its policy
  Denies `s3:GetObject` on any object carrying `STORAGE_QUARANTINE_TAG_KEY=true`
  (`core/constants.py`), so `takedown_service.quarantine_file` tagging an object revokes an
  already-minted presigned URL immediately — no new mint required. **Never grant this
  identity tagging rights** (`s3:PutObjectTagging`/`s3:DeleteObjectTagging`) or
  `s3:ListBucket` — the identity a Deny is keyed on must not be able to clear its own key,
  and measured least-privilege containment depends on it staying that way. MinIO-only (no
  admin API on native S3) and fails open to the root client if the identity can't be
  provisioned — see `storage_presign_identity.py`'s module docstring and
  `docs/abuse-and-takedown.md`. On native S3 revocation needs an operator-attached bucket-policy
  Deny; `report_native_s3_revocation_posture` checks for it once at startup and WARNs if absent
  (exposure = `MEDIA_URL_EXPIRE_SECONDS`). `presign_client()` logs ERROR **only** for a MinIO
  provisioning failure — never on S3 or when disabled, where it fired from every process (#1005).

- **Orphaned objects are swept, not assumed gone (#1212).** A file's objects all live under
  `user_<uid>/file_<fid>/` (original, thumbnails, `.playback.m4a`, derived/masked caches,
  waveform). The app's delete paths remove them with the row, but a row removed out of band
  (raw SQL, a restore or reset against a persisted bucket) strands them.
  `tasks/storage_orphan_sweep_task.py` finds and removes them: dry run first, empty-Postgres
  refusal, 10% ratio guard (`force`), per-file Postgres re-check immediately before deleting,
  1 h grace period, and it ignores every key that is not that shape (`media/...`,
  `user_<uid>/youtube_<id>/...`, exports, avatars, other buckets). A NEW key shape for
  per-file objects must keep the `user_<uid>/file_<fid>/` prefix or the sweep cannot see it
  (and a file's objects would outlive it unnoticed).

## LLM features (optional)

`llm_service.py` is a **synchronous** client on purpose (Celery tasks; no asyncio conflicts).
`LLMProvider` = openai · vllm · ollama · anthropic · openrouter · bedrock · custom (`claude` is a
deprecated alias for `anthropic`).

- **Resolution order**: `create_from_user_settings(user_id)` → falls back to
  `create_from_system_settings()` (env `LLM_PROVIDER` + provider keys/endpoints). Empty
  `LLM_PROVIDER` and no user config = transcription-only; `create_from_system_settings`
  returns `None` and callers must handle it. `custom` is **user-config only** — it always
  returns `None` from system settings.
- **"Is an LLM configured?" has ONE answer: `LLMService.is_configured_for_user(db, user_id)`**
  (a thin wrapper over `describe_for_user`, which also returns `(provider, model, base_url)` for
  the pipeline view's AI-features step).
  DB-only, no network, and built from the same helpers the factories use
  (`_resolve_user_llm_settings`, `_resolve_system_provider`, `_endpoint_resolves`), so it is
  true exactly when `create_from_settings` would return a service. Never re-derive it as
  `bool(settings.LLM_PROVIDER)`: a provider with no key or endpoint passes that check while
  every task skips, which made the post-transcription sweep re-dispatch topic extraction for
  every file every 10 minutes (issue #1017).
- **Automatic LLM steps have per-user opt-outs (#1148).** `auto_llm_speaker_identification` and
  `auto_topic_extraction` (default ON; `UserSetting` rows, system tier `auto_llm.speaker_identification`
  / `auto_llm.topic_extraction`) are resolved ONLY by `services/auto_llm_settings.py`
  (user > system > True). The AUTOMATIC dispatch sites consult it: `_dispatch_llm_speaker_identification`
  in `speaker_attribute_task.py` (every chain exit, `dispatch_speaker_attribute_pipeline`, and so
  postprocess/background/rediarize), `trigger_automatic_summarization` in `transcription/downstream.py`
  (only when `tasks_to_run is None`), `ai_insights_companion.queue_topic_extraction_with_summary`
  (a manual Generate/Regenerate summary also queues topic extraction for a file that never had a
  `TopicSuggestion`; Redis-guarded against double clicks, fails closed), and `TaskDetectionService.identify_incomplete_post_transcription_files`
  (the recovery sweep: a step the user turned off must not be flagged missing, or it is re-queued and
  re-detected every 10 minutes). Manual triggers and an explicit stage list (`speaker_llm` /
  `topic_extraction` via `dispatch_task_by_name` or `llm_requested=True`) never consult it. A new
  automatic dispatch of either step must go through the resolver.
- **`bedrock` is the one SDK-based provider** (`SDK_PROVIDERS`, `llm_bedrock.py`): boto3's
  Converse API, not an HTTP endpoint, no `base_url`. A configuration carries its OWN region and
  optional AWS credentials (access key id, secret, session token), stored as ONE encrypted JSON
  blob in the row's `api_key` column (no migration; `models.UserLLMSettings.has_api_key` is
  False for Bedrock and `aws_region` / `has_aws_credentials` are the only things a response
  sees). `services/bedrock_settings.py` is the ONLY place that decides a Bedrock region and
  builds boto3 client kwargs (`resolve_region`, `client_kwargs`, `merge_stored`, `scrub`,
  `describe_failure`); `LLMService.credentials_from_row` is the one decryption point and puts the
  result on `LLMConfig.bedrock` (`BedrockConnection`, secrets excluded from repr). Blank
  credentials mean the server's boto3 chain and `settings.BEDROCK_REGION` (which `Settings`
  already folded from `AWS_REGION` / `AWS_DEFAULT_REGION`); the environment-selected system
  fallback (`_get_provider_config`) uses `env_region()` and `resolve_default_model()`. Nothing
  in `bedrock_settings` touches the DB, so a Celery task resolves a config once, closes its
  session and calls Bedrock with none open. `tests/unit/test_bedrock_connection.py` pins it
  structurally: no `boto3.client("bedrock...")` outside `llm_bedrock._client`, no
  `settings.BEDROCK_*` read outside the resolver, no credential kwarg elsewhere. The credentials
  are write-only on every path: `GET /config/{uuid}/api-key` and the model-discovery key lookup
  both refuse a Bedrock row, error text is `scrub`bed, a connection test answers
  `FAILURE_MESSAGES` sentences only, and `core/validation_errors.py` drops `input` from a 422
  that concerns a sensitive field. `GET /llm-settings/bedrock/server-credentials` (admin) reports
  whether the server's own chain resolves, by source KIND. Reasoning-probe support is
  deliberately **not** wired (`llm_reasoning.PROBEABLE_PROVIDERS` excludes it) — see that section
  below for why a provider needs a live measurement first, not just an enum entry.
- Per-user keys are AES-encrypted (`utils/encryption.encrypt_api_key`) and never returned;
  edit mode reuses the stored key when the request omits `api_key`.
- Summarization: BLUF, speaker analysis with talk time, action items, decisions, follow-ups,
  multi-section stitching for long transcripts (`_chunk_transcript_intelligently` →
  `_summarize_section` → `_combine_sections`). Output languages: `core/constants.py:
  LLM_OUTPUT_LANGUAGES` (12: en es fr de it pt **nl** ru zh ja ko ar — no Hindi).
- **Never log prompt or model-output text at INFO or above (issue #1022).** Output is derived
  from the transcript, so it would copy transcript content into log aggregation. On a parse
  failure log `utils/llm_log_safety.describe_llm_text(text)` (`len=N sha256=<12 hex>`) and, if
  an excerpt helps debugging, `log_llm_text_excerpt` (DEBUG only). Watch for indirect leaks:
  a pydantic `ValidationError`'s message embeds its `input_value`, so log `type(e).__name__`.
  `tests/unit/test_llm_parse_failure_log_hygiene.py` drives each parse-failure path.
- **Speaker suggestions are never auto-applied.** `identify_speakers` returns confidence-scored
  predictions stored for manual verification (`tasks/speaker_identification_task.py`). Only
  tags/collections have an auto-apply path (`auto_label_service.auto_apply_suggestions`).

### Reasoning is a per-MODEL capability, and it is MEASURED (`llm_reasoning.py`, issue #64)

**A provider returning HTTP 200 for a "do not reason" parameter is not evidence the model
honoured it.** Measured against the live vLLM (`localhost:5195`) serving **gemma-4-e4b**, at
**temperature 0, max_tokens 1200**, summing **both** response spellings:

| arm | reasoning | content | tokens |
|---|---|---|---|
| `chat_template_kwargs={"enable_thinking": true}` | 1656 | 843 | 1123 |
| `chat_template_kwargs={"enable_thinking": false}` | **931** | 378 | 562 |
| kwarg omitted (the control) | **931** | 378 | 562 |

Three facts, all load-bearing:

1. **`false` is byte-identical to omitting the kwarg.** The chat template ignores the off value.
   vLLM answers **200** and silently does nothing — same shape as #437 and the
   `_neural_pipeline_verified` bool.
2. **Reasoning is produced in ALL THREE arms.** gemma-4-e4b has no off-switch at all.
3. **`true` does change behaviour**, which is why `_prepare_openai_payload` still sends
   `enable_thinking: true` unconditionally for vLLM.

The numbers are true of *that model on that server*. A different model is precisely the case
this design exists to serve, so nothing keys off the provider alone.

**⚠️ THE METHOD TRAP: sum BOTH `reasoning` AND `reasoning_content`.** vLLM 0.19 reports
`reasoning`; the parser convention and several gateways report `reasoning_content`. An earlier
probe read only one, measured ~0 on every arm, and reported the behaviour as *non-deterministic*
— it is perfectly deterministic. `llm_reasoning.reasoning_chars()` is the one reader; use it.

**⚠️ The probe is NON-streaming, deliberately.** vLLM's *streaming* reasoning parser only enters
reasoning mode on the template's opening token (that is #439), so a streaming probe measures the
**parser** and would report a working off-switch for every model. The non-streaming parser
separates the block in all three arms, which is what makes the comparison about *generation*.
`LLMService.chat_completion_raw` exists for this: `chat_completion` raises on empty content, and
the activated arm legitimately returns `content: null` with 1,656 characters of reasoning.

#### The pieces

| What | Where |
|---|---|
| Probe, verdict rule, storage helpers | `services/llm_reasoning.py` |
| Verdict enum (3 layers read it) | `core/enums.ReasoningOffSwitch` |
| Instrument settings (prompt, tolerance, floor, timeouts) | `core/constants.py`, `LLM_REASONING_*` |
| Recorded verdict | `SystemSettings`, key `llm.reasoning.off_switch.<fp>` |
| Run it | `POST /llm-settings/config/{uuid}/reasoning-probe` |
| Read it | `GET /llm-settings/config/{uuid}/reasoning`, and `reasoning_off_switch` on the configurations list |
| Applied to a turn | `endpoints/chat/messages.py` → `llm_reasoning.resolve_enable_thinking` |
| UI condition | `ChatControlsPanel.svelte`, renders only on `'works'` |

#### Adding a model (or a provider) — the repeatable procedure

1. Configure it as an LLM configuration and press the probe (or `POST .../reasoning-probe`).
   It runs three real generations at temperature 0 against **that** endpoint: `true`, then
   `false`, then the parameter omitted.
2. Read the verdict. `works` → the toggle appears. `absent` → probed, and the model ignored it.
   `no_reasoning` → nothing to switch off. `unsupported` → this build has no off-switch
   parameter for that provider. `unknown` → never probed, or the probe could not complete.
   Every value except `works` renders no control and leaves the request as it is today.
3. **A new PROVIDER needs two lines and a measurement**: an entry in
   `llm_reasoning.PROBEABLE_PROVIDERS`, and the parameter in the matching
   `LLMService._prepare_*_payload`. Ollama's native `think` and Anthropic's `thinking.type` are
   real mechanisms and are deliberately **not** wired up: adding one without measuring it is
   exactly the mistake this module exists to prevent. `custom` must never be added — those
   OpenAI-clones **400 on unknown payload keys** (the same reason they are excluded from
   `llm_stream.USAGE_OPTION_PROVIDERS`).

**The tolerance, and why.** `off` must be ≤ **10%** of the omitted control *and* ≤ 10% of the
activated arm; below `LLM_REASONING_PROBE_MIN_CHARS` (32) in every arm it is `no_reasoning`
instead. 90% suppression is the weakest threshold under which the word "off" is honest — a
switch that halves the thinking still leaves the user reading a false claim. Both conditions are
required: the first catches the measured failure (`off == omitted`), the second stops a
coincidentally-quiet control run from scoring a switch. The measured negative sits at 1.00 of
the control and 0.56 of the activated arm, i.e. **5.6× clear** of the boundary on the tighter
condition.

#### Where the verdict is recorded, and why there

`SystemSettings`, keyed by a **fingerprint of (provider, base_url, model)** — not a column on
`user_llm_settings`. The capability belongs to the software answering at that URL, not to a
config row: two users pointing at the same vLLM share one measurement instead of paying three
generations each, and editing a config's model produces a key that was never written, so the old
verdict is simply *unfindable* rather than stale. No migration, and no invalidation logic. The
URL is hashed rather than stored so the deployment-wide table does not become a directory of
everyone's private endpoints.

It is a **measurement, not a setting** — no coded default is editable into it, no admin panel
writes it, and the row's `description` says so, because an operator hand-editing it would be
asserting a capability nobody measured.

**When it runs: explicit user action only.** Three real generations is far too much in front of
a user's first chat turn, and a background sweep would dial every configured third-party
provider unprompted — an egress event, and a bill, nobody asked for. It sits beside "Test
connection", where the cost is visible and chosen.

#### ⚠️ #439's fix must stay, and a model with no off-switch must behave exactly as today

`_prepare_openai_payload` has three arms and the **default is unchanged**: absent or `True` →
`{"enable_thinking": True}` (the #439 fix), `False` → `{"enable_thinking": False}` (the measured
off arm), `None` → the key omitted (the probe's control arm, nothing on the chat path passes it).
Do not "clean up" that unconditional `true`: without it vLLM's streaming parser never engages and
the whole chain-of-thought is rendered as the answer, closed by a literal `<channel|>` token.

`resolve_enable_thinking` returns `None` — i.e. *change nothing* — unless the verdict is `works`.
So a stored "reasoning off" preference on a model whose off-switch was never measured is
deliberately **not applied**; it applies again if the conversation is later pointed at a model
that can honour it. `tests/unit/test_llm_reasoning_capability.py` pins that non-regression, with
a `works` control beside it so "always return None" cannot pass.

### The context window is MEASURED too (`llm_context_window.py`, issues #533 / #833)

`LLMConfig.max_tokens` **is** the context window (`LLMService.__init__`), and it used to be
whatever the user *declared* in `UserLLMSettings.max_tokens` — never checked against what the
endpoint actually serves. Issue #533 built the probe (a real, opt-in measurement, stored the
same way as the reasoning verdict above); issue #833 is what actually **applies** it on the
chat/summarization runtime path. Before #833, a declared value above the real ceiling produced
either a hard 400 (vLLM/OpenAI-compatible) or silent front-of-context truncation (Ollama sends
the declared value as `num_ctx`, which can drop the system prompt first) — and issue #873
raising the declared Ollama default to 128000 made an over-declared config the common case, not
the rare one.

| What | Where |
|---|---|
| Probe, storage helpers, the narrowing rule | `services/llm_context_window.py` |
| Status enum | `core/enums.ContextWindowStatus` |
| Recorded measurement | `SystemSettings`, key `llm.context_window.<fp>` |
| Run it | `POST /llm-settings/config/{uuid}/context-window-probe` |
| Read it | `GET /llm-settings/config/{uuid}/context-window` |
| Applied to a turn | `LLMService.create_from_*` → `llm_context_window.effective_window` |

**It only ever narrows.** `effective_window` mirrors `resolve_enable_thinking`'s "return
unchanged unless the verdict is definitively known" rule: no measurement record (the probe is
opt-in, so this is the common case), a non-`MEASURED` status, or a measured value >= the
declared one all return the declared value unchanged. Only a measured value *below* the declared
one is applied, with a warning log naming both numbers — a declared value below the measured
ceiling may be a deliberate VRAM/cost/shared-server decision, and raising it automatically would
override that decision and reintroduce the failure this exists to prevent.

**`LLMConfig.max_tokens` on a built service is therefore the EFFECTIVE window, while the
`user_llm_settings` row keeps the DECLARED one** — the same split issue #64 draws between a
stored preference and what the runtime actually honours. `LLMService.__init__` copies
`config.max_tokens` into `self.user_context_window`, so every downstream budget computation
(response token sizing, chunking, the Ollama `num_ctx` payload, `resolve_answer_tokens` /
`build_messages` in `chat/prompting.py`) is narrowed automatically without a second call site.
`chat/service.py` records the effective value actually used on `turn.metadata["context_window"]`
after `build_messages`, so a truncated turn is attributable rather than reading as an unexplained
retrieval failure.

No pre-flight token-count validator was added on top of this: once the budget is computed
against the real (possibly narrowed) window, prompt + answer fit by construction, and a second
token-counting check would duplicate logic `build_messages` already owns.

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
YouTube PO tokens; `_YOUTUBE_EXTRACTOR_ARGS` rotates player clients).

- Limits: **15 GB** (`max_filesize`, matches the upload limit) and **4 h** (`duration > 14400`).
- `create_user_friendly_error` maps raw yt-dlp errors → guidance via `AUTH_ERROR_PATTERNS` +
  `PLATFORM_GUIDANCE`. `RECOMMENDED_PLATFORMS = ["YouTube", "Dailymotion", "Twitter/X"]`.
  Vimeo / Instagram / Facebook / LinkedIn / Patreon usually need auth.

## Exports and redaction — three rules that are not obvious (issue #85)

`subtitle_service.build_subtitle_archive` (the bulk ZIP) and the burned-in-subtitle render
in `video_processing_service` used to call the subtitle generators with **no redaction
config**, exporting the raw transcript for everyone — including under the admin
`redaction.force_export_redacted` floor, which the UI presents as covering exports.

- **A "disabled" policy and an absent one must not be the same value.** `cfg=None` read as
  "redaction is off" to `_redact_segments_inplace`, and that is exactly what "the caller
  forgot" looks like, so no gate could ever fire on it. The parameter is now required and
  non-`Optional` on both paths; a disabled policy is an `EffectiveRedactionConfig` whose
  `enabled` is False. There is no argument value meaning "unspecified".
- **⚠️ The subtitle masker mutates the loaded ORM objects, and both export workers COMMIT.**
  `session_scope` commits on exit, so wiring masking into those tasks would have flushed the
  masked text into `transcript_segment.text` and destroyed the original transcript — the one
  thing read-time masking exists to preserve. The API path never noticed because `get_db`
  closes without committing. `_redact_segments_inplace` now **expunges each segment before
  mutating it**, so the guarantee is structural rather than a property of whichever caller
  holds the session. (`formatting_service._apply_redaction` was never exposed: it masks a
  dict copy.) Pinned by `test_export_redaction_paths.py`'s
  `test_the_export_never_writes_the_masked_text_back`, which is red without the expunge.
- **A cached artifact must name the policy it was rendered under.** Burned-in text cannot be
  masked afterwards, so `generate_cache_key` takes a required keyword-only
  `redaction_fingerprint`; without it the first reader's render is served to everyone, and an
  admin enabling the floor would keep serving an already-cached unmasked video. Consequence:
  `derived_cache_keys` can no longer enumerate a file's objects, so `clear_derived_cache`
  **lists** the masked variants — a delete that only reads that list leaves a video of the
  transcript in storage.

Whose policy applies (the **requesting user**, not the file owner) and when it is resolved
(**run time inside the task**, from a `user_id`, never a serialized config) are argued in
`redaction/export_policy.py`.

**And one rule that is not about redaction at all (issue #818): the export workers re-check
QUARANTINE at run time.** Both entry points are permission-filtered at dispatch, but a
takedown can land in the gap — `build_subtitle_archive` takes `include_quarantined` and
*skips* a taken-down file like any other unusable entry (one file must not fail the other
99), while `prepare_media_download_task` wraps its own file read in `exclude_quarantined` so
a taken-down file is refused exactly as a deleted one is. Sharing/tenant scope is still
decided once, at the endpoint; only the abuse gate, which is time-varying by design, is
re-applied.

## Gotchas

- Settings that look like env vars are frequently **DB-backed** (`SystemSettings` /
  `UserSetting`) with coded defaults in `core/constants.py`. Check before adding an env var.
- GPU worker, CPU worker, and `celery-redaction` load different models — importing a
  model-loading service into a task on the wrong queue pulls weights onto the wrong device.
- Docs: `docs-site/docs/features/llm-integration.md`,
  `docs-site/docs/features/transcription.md`,
  `docs-site/docs/configuration/neural-search-setup.md`.
