# #1280 follow-up: record whether a speaker's name came from a person or a voiceprint. Plan

Status: **PLAN ONLY.** Written 2026-10-27 against `feat/privacy-controls-1280` @ `f63b4043`.
Line numbers drift; search for the quoted symbol. Parent design: `docs/design/v060_privacy_controls_plan.md`
(D5 "switching off hides", §3.3 purge). Owner decisions: `docs/design/v060_owner_decisions.md`.

Implementation lanes (already created, both at `f63b4043`):

| Lane | Worktree | Branch | Owns |
|---|---|---|---|
| B (writes + purge + migration) | `.claude/worktrees/lane-pfb` | `fix/privacy-writes-purge-1280` | Phase 0, then §6 |
| A (read surfaces) | `.claude/worktrees/lane-pfa` | `fix/privacy-surfaces-1280` | §5, after merging Phase 0 |

## 1. The defect (verified)

- `services/speaker_matching_service.py` `_handle_speaker_match`: when `match["auto_accept"]`
  (confidence >= `ConfidenceLevel.HIGH` = `SPEAKER_CONFIDENCE_HIGH` = 0.75) it writes
  `display_name`, `verified = True` and `profile_id`, exactly what a person's confirmation writes.
- The same shape is written by three more machine paths:
  `_propagate_profile_assignment` (`profile_id`, `verified = True` on similar speakers),
  `api/endpoints/speaker_update.py` `_apply_high_confidence_match` (retroactive matching:
  `display_name`, `verified`, `profile_id` from a cosine >= 0.75), and
  `services/speaker_cleanup.py` `join_cluster_identity` when called from
  `speaker_clustering_service._join` (upload-time `find_or_create_cluster` and Re-cluster Phase 2.5,
  both kNN on voiceprints).
- The privacy code calls "machine-derived" only `verified IS NOT TRUE` voice suggestions:
  `services/privacy/redact_view.py` `hides_voiceprint_suggestion` and `services/privacy/purge.py`
  `purge_voiceprints_postgres` (`or_(Speaker.verified.is_(None), Speaker.verified.is_(False))`).
  So the commonest voice identification is neither hidden (switch off) nor removed (purge).
- A second, related gap found while planning: **no derived plane consults the policy at all.** The
  chunk index (`tasks/search_indexing_task.resolve_chunk_speaker_name`, `tasks/reindex_task.
  _resolve_reindex_speaker_name`, and the `profile_id` field both write), digests/facts
  (`services/ingest_artifacts/service.py` via `transcript_builders.get_speaker_name`), LLM prompts
  (`tasks/summarization.py`, `tasks/speaker_identification_task.py` via `utils/transcript_builders`),
  exports (`transcript_export_service.build_export_content`, `transcript_export_turns.speaker_namer`),
  captions (`captions/source.speaker_labels_for`), the chat roster
  (`chat/speaker_resolver.build_candidate_roster`), the gallery speaker chips and filter
  (`files/__init__.py` list options, `files/filtering.py` `Speaker.display_name == s`,
  `media_collections.py`) and the speaker name directory (`speakers.py`, the `label_col` query) all
  show a voice-made name (and an unverified >= 0.75 voice suggestion, which the canonical label
  promotes) while the switch is off. Only API responses that pass through `redact_speaker_rows` hide
  anything today. This plan fixes both gaps with one mechanism (fold, don't defer).

## 2. Decisions

**D1. A new nullable column `speaker.identity_source`, not `suggestion_source`.**
`suggestion_source` describes `suggested_name`, not `display_name`, and cannot carry this:
- A person accepting a voice suggestion in the Inbox (`batch_verify_speakers` `accept`, also the
  single-speaker Inbox path with `override_unassignment`) writes `display_name = suggested_name`,
  `verified = True` and leaves `suggested_name`/`confidence`/`suggestion_source` in place: the row is
  byte-identical to an auto-accept of a `voice_match`.
- `task_detection_service` reads `suggestion_source` standalone as "LLM speaker ID ran"
  (`speakers._apply_verification_on_display_name` docstring); repurposing it breaks that.
- `skip` writes `user_skipped` into it; the LLM task writes `llm_analysis`.

Values (CHECK `ck_speaker_identity_source`, `VARCHAR(16)`):

| Value | Meaning | Hidden while voiceprints off | Purge |
|---|---|---|---|
| `voiceprint` | name AND link (and `verified`) written by a voiceprint path | name, link, verified, voice suggestion | revert to unnamed/unlinked |
| `voiceprint_link` | only the profile link is machine; the name was already there | link only | unlink, keep the name |
| `human` | a person typed, accepted, assigned or confirmed it | nothing | nothing |
| `NULL` | written before v449 (unknown), or never named | nothing (treated as human) | nothing |

`human` is recorded (not left NULL) so a later, opt-in classification of legacy rows remains
possible: NULL keeps meaning "we do not know". Readers treat `human` and NULL identically.

**D2. Which sites set it** (Lane B implements; all through `app/utils/speaker_identity_source.py`):

| Site (file :: function) | Call |
|---|---|
| `speaker_matching_service :: _handle_speaker_match` (auto_accept branch) | `record_machine_identity(speaker, previous_display_name=<value before the write>)` |
| `speaker_matching_service :: _propagate_profile_assignment` (each updated speaker) | `record_machine_link(speaker)` |
| `speaker_update :: _apply_high_confidence_match` | `record_machine_identity(speaker, previous_display_name=...)` |
| `speaker_cleanup :: join_cluster_identity` | new required kw-only `source: Literal["voiceprint", "human"]`; `voiceprint` -> `record_machine_identity`, `human` -> `record_human_identity` |
| `speaker_clustering_service :: _join` (upload `find_or_create_cluster`, Phase 2.5) | passes `source="voiceprint"` |
| `speaker_clustering_service :: merge_clusters` (its `join_cluster_identity` call) | `source="human"` (a person merged the clusters) |
| `speaker_clustering_service :: promote_cluster_to_profile` (members) | `record_human_identity` (owner decision Q2) |
| `speaker_clustering_service :: batch_verify_speakers` `accept` / `name` / `assign` | `record_human_identity`; `skip` changes nothing |
| `speakers.py :: create_speaker` (display_name given), `update_speaker` (display_name set, or a `profile_action`), `_handle_create_new_profile_action`, `_accept_speaker_profile_match`, `_create_new_speaker_profile` | `record_human_identity` |
| `speaker_matching_service :: assign_speaker_to_profile`, `speaker_update :: auto_create_or_assign_profile` | `record_human_identity` |
| `speaker_cleanup :: unlink_from_profile` | `after_unlink(speaker)` (see helper) |
| `speakers.py :: _reject_speaker_suggestion` | no direct call; its `unlink_from_profile` applies the rule |
| `speaker_profile_rename` (renames linked speakers to the new profile name) | unchanged on purpose: renaming the profile does not confirm a machine link. Allowlisted in the discipline test |
| LLM speaker ID, `batch_speaker_matching.py`, `_handle_no_speaker_match` | suggestion fields only, no identity write: unchanged |
| diarization row creation (`speaker_processor.py`), reprocess/rediarize | new rows, NULL: correct |

Helper semantics (`app/utils/speaker_identity_source.py`, pure, no DB):
- `record_machine_identity(speaker, *, previous_display_name)`: keep `voiceprint` if already;
  else `voiceprint_link` when `is_assigned_speaker_name(previous_display_name)` and it equals the new
  `display_name` case-insensitively (the machine only added the link); else `voiceprint`.
- `record_machine_link(speaker)`: keep `voiceprint` if already; else `voiceprint_link`.
- `record_human_identity(speaker)`: `human`.
- `after_unlink(speaker)`: if `display_name` is now empty -> NULL; elif source is `voiceprint_link`
  -> NULL (the remaining name predates the link); else unchanged.
- `hides_name(source, enabled) = not enabled and source == "voiceprint"`;
  `hides_link(source, enabled) = not enabled and source in MACHINE_IDENTITY_SOURCES`.
Known accepted edge: `_handle_speaker_match` can overwrite a different, human-typed name on a
re-match (existing behaviour, unchanged here); that row becomes `voiceprint`.

**D3. Existing rows: unknown = human, plus one provable shape.** Evidence, from every writer of
`suggestion_source = 'profile_match'` (`rg "profile_match\""`: only `_handle_speaker_match`, and
migration `v439`): the live writer stores a `profile_match` suggestion only at confidence >= 0.75 and
`auto_accept` is the same comparison, so every live `profile_match` row was auto-accepted at write
time. A later human rename (`update_speaker` -> `_apply_verification_on_display_name`) clears
`suggested_name`, so a row still showing the shape has not been renamed. The backfill stamps
`voiceprint` only where ALL hold:

```sql
s.identity_source IS NULL
AND s.suggestion_source = 'profile_match'
AND s.verified IS TRUE
AND s.profile_id IS NOT NULL
AND coalesce(s.suggested_name, '') <> ''
AND s.display_name = s.suggested_name
AND EXISTS (SELECT 1 FROM speaker_profile p WHERE p.id = s.profile_id AND p.name = s.display_name)
```

Residual false positives (disclosed, owner decision Q1): rows `v439` stamped from pre-`v091` data
where a person had linked the same-named profile, and a person who re-assigned the identical profile
afterwards in a batch. Everything else stays NULL (= human) because Postgres cannot tell it apart:
`voice_match` auto-accepts (identical to Inbox accepts), propagation links, retroactive matches and
upload cluster joins. Confidence is NOT in the predicate: a re-match can lower `confidence` without
touching the suggestion. Upgrade notes state this (§6.6).

**D4. Migration: `v449_add_speaker_identity_source`.** Head derivation:
`python3 scripts/release-tests/lib/alembic-head.py backend` -> `v447_add_content_hold` on this
branch. `fix/v060-wave1-integration` (and `fix/v060-wave1-audit-fixes`, `fix/v060-tenant-write-paths`,
`fix/task-row-recovery-1185`, `fix/v060-prompt-share-tenant-scope`) already hold
`v448_task_one_active_row_per_file_step` (`down_revision = v447`). No branch holds v449+
(checked every local ref and worktree on 2026-10-27). So: **number v449** (never v448: a second
v448 merges textually clean and forks the graph). Parent:
- If wave-1 has been merged into `feat/privacy-controls-1280` before Phase 0 starts (check
  `ls backend/alembic/versions | rg v448`), `down_revision = "v448_task_one_active_row_per_file_step"`.
- Otherwise `down_revision = "v447_add_content_hold"`, and whoever merges the second integration
  branch re-points it to v448 in the merge commit (revision file, `test_v449_migration_consistency.
  test_v449_revision_chain`, and the detection-ladder order). `alembic-head.py` exits non-zero on a
  fork, so run it after every merge.

Shape (raw idempotent SQL, module-level constants so the consistency test replays them):

```sql
-- ADD_COLUMN_SQL: nullable, no default => metadata-only, no table rewrite
DO $$ BEGIN IF NOT EXISTS (SELECT 1 FROM information_schema.columns
  WHERE table_name = 'speaker' AND column_name = 'identity_source')
THEN ALTER TABLE speaker ADD COLUMN identity_source VARCHAR(16); END IF; END $$;
-- ADD_CHECK_SQL: NOT VALID => brief lock, no scan
DO $$ BEGIN IF NOT EXISTS (SELECT 1 FROM pg_constraint WHERE conname = 'ck_speaker_identity_source')
THEN ALTER TABLE speaker ADD CONSTRAINT ck_speaker_identity_source CHECK (identity_source IS NULL
  OR identity_source IN ('voiceprint', 'voiceprint_link', 'human')) NOT VALID; END IF; END $$;
-- VALIDATE_SQL: SHARE UPDATE EXCLUSIVE, reads/writes continue
ALTER TABLE speaker VALIDATE CONSTRAINT ck_speaker_identity_source;
-- BACKFILL_SQL: UPDATE speaker s SET identity_source = 'voiceprint' WHERE <D3 predicate>
```

No index (purge and resync filter by scope first; `CREATE INDEX` inside the migration transaction
would block writes on a large table). Downgrade: drop the constraint and the column (`IF EXISTS`);
names are untouched. Docstring: why, the D3 evidence and residue, community edition = same
behaviour. Detection arm at the TOP of `_detect_schema_version()` (`app/db/migrations.py`):
`has_identity_source = _check_exists("SELECT EXISTS(SELECT 1 FROM pg_constraint WHERE conname =
'ck_speaker_identity_source')")`, returning v449 when it and the v447 (or v448, after the merge)
fingerprint hold; the v447 arm comment "newest revision on this chain" moves to v449.

**D5. Reads hide at read time; derived planes are rebuilt.** Postgres-backed reads decide per request
(`PrivacyView`, record scope = the file's owner + organization, fail closed). Stored planes
(`transcript_chunks` labels + `profile_id`, digests, facts) are rebuilt from Postgres by the existing
per-file reindex (`search/reindex_dispatch.queue_transcript_reindex`), triggered (a) when a switch
flips (new `privacy/label_resync.py`), (b) by a rename in an off scope (§5.4), (c) by a purge (the
existing `refresh_cleared_speakers`). Persisted `speaker.resolved_display_name` / `computed_status`
stay POLICY-FREE (they describe the data); responses recompute them on the redacted row.
Not covered, disclosed: LLM summary/topic text already generated may contain the name (it is model
output, not a label); regenerating the summary while off produces one without it.

**D6. Turning the switch back on restores everything.** Nothing is deleted by hiding; the read-time
paths show the name on the next request and the resync re-indexes the same files. Intended (D5 of the
parent plan) and tested (§7, A10).

## 3. Phase 0: the shared contract (Lane B's agent, one commit, BEFORE the fan-out)

Commit on `fix/privacy-writes-purge-1280`:
`feat(privacy): record whether a speaker's identity came from a voiceprint (#1280)`.

0.1 `backend/alembic/versions/v449_add_speaker_identity_source.py` (D4).
0.2 `app/models/media.py` `Speaker`: `identity_source: Mapped[str | None] = mapped_column(String(16),
    nullable=True)` and `CheckConstraint(..., name="ck_speaker_identity_source")` in `__table_args__`
    (same text as the migration; `test_orm_ddl_divergence.py` must stay green).
0.3 `app/db/migrations.py` detection arm (D4). Widen `tests/unit/test_v447_migration_consistency.py`
    only if it pins the head or detection exactly (tests/CLAUDE.md: "each new migration breaks the
    previous suite's detection assertion").
0.4 New `app/utils/speaker_identity_source.py`: constants `VOICEPRINT`, `VOICEPRINT_LINK`, `HUMAN`,
    `IDENTITY_SOURCES`, `MACHINE_IDENTITY_SOURCES`, and the D2 helpers.
0.5 `app/utils/speaker_labels.py`:
    - `canonical_speaker_label(..., identity_source: str | None = None, voiceprint_names: bool = True)`.
      When `voiceprint_names` is False: skip `display_name` if `hides_name(identity_source, False)`,
      and never promote a voiceprint suggestion (EMBEDDING_SUGGESTION_SOURCES). Order otherwise
      unchanged.
    - `canonical_speaker_label_for_row(row, *, voiceprint_names: bool = True)`: when False, the row
      MUST expose `identity_source` (raise `TypeError("row lacks identity_source; select it")`), so a
      raw-column query that forgot the column fails loudly instead of leaking.
    - `visible_profile_id(profile_id, identity_source, voiceprint_names) -> int | None`.
0.6 `app/services/privacy/redact_view.py`:
    - `hides_voiceprint_suggestion(speaker, voiceprints_enabled=False)`: also true when
      `speaker.identity_source == "voiceprint"` (a machine-verified row's suggestion is the evidence).
    - New `hide_machine_identities(view, speakers)`: for each row whose file is off,
      `set_committed_value` `display_name=None`, `verified=False` when `hides_name`; `profile_id=None`
      and `profile=None` when `hides_link`. Touches only `identity_source`, `media_file_id`,
      `display_name`, `profile_id`, `verified`, so it is safe on `load_only` rows (gallery lists).
    - `redact_speaker_rows`: call `hide_machine_identities` first, then the existing suggestion and
      gender rules, then, for every row it changed, recompute
      `SpeakerStatusService.compute_speaker_status(row)` on the redacted row and `set_committed_value`
      `computed_status`, `status_text`, `status_color`, `resolved_display_name`.
    - `PrivacyView.label_for_row(row) -> str` = `canonical_speaker_label_for_row(row,
      voiceprint_names=self.voiceprints(row.media_file_id).enabled)`.
    - SQL: `machine_identity_clauses(view, file_ids_select) -> tuple[name_hidden, link_hidden]`
      (`ColumnElement[bool]`). Reads the DISTINCT `(MediaFile.user_id, MediaFile.organization_id)` of
      files in `file_ids_select` that have a machine identity, keeps the off scopes, and builds
      `or_(and_(MediaFile.user_id == u, MediaFile.organization_id.is_(None) if o is None else == o))`
      joined to `Speaker.media_file_id`. Never `tuple_(...).in_()`: `(u, NULL) IN (...)` is NULL and
      would leak every personal-scope row. Returns `(false(), false())` when nothing is off.
      Plus `visible_display_name_expr(name_hidden)` / `visible_profile_id_expr(link_hidden)`
      (`case((hidden, None), else_=column)`).
0.7 Tests T0.1-T0.3 (§7). Lane A then runs `git merge --no-ff <phase0-sha>` in its worktree.

## 4. Ownership (no file is edited by both lanes)

| Lane A (surfaces) | Lane B (writes, purge, its own files' reads) |
|---|---|
| `app/tasks/search_indexing_task.py`, `app/tasks/reindex_task.py`, `app/tasks/rename_propagation_task.py` | Phase 0 files (§3) |
| `app/utils/transcript_builders.py`, `app/tasks/summarization.py`, `app/tasks/speaker_identification_task.py` (prompt build only), `app/services/ingest_artifacts/service.py` | `app/services/speaker_matching_service.py` |
| `app/services/transcript_export_service.py`, `app/services/transcript_export_turns.py`, `app/services/captions/source.py` | `app/api/endpoints/speaker_update.py` |
| `app/services/chat/speaker_resolver.py` | `app/api/endpoints/speakers.py` (writes + the name directory query) |
| `app/api/endpoints/files/__init__.py`, `files/filtering.py`, `files/crud.py`, `app/api/endpoints/media_collections.py`, `app/services/formatting_service.py` | `app/services/speaker_cleanup.py`, `app/services/speaker_clustering_service.py` |
| `app/api/endpoints/speaker_profiles.py` (counts) | `app/services/privacy/purge.py` |
| new `app/services/privacy/label_resync.py`, new `app/tasks/privacy_label_resync_task.py`, `app/core/celery.py`, `app/services/privacy/policy_admin.py`, `app/tasks/search_maintenance_task.py` | `docs-site/docs/features/privacy-controls.md`, `CHANGELOG.md`, `backend/app/services/CLAUDE.md` |
| `backend/app/services/search/CLAUDE.md`, `backend/app/api/CLAUDE.md`, `docs/design/v060_downstream_changes.md` | |
| tests A1-A11 | tests B1-B6 |

`speaker_identification_task.py` line ~331 (tracker capture) is Lane A's file; it changes only the
prompt-builder calls. Each lane commits only in its own worktree (root CLAUDE.md, one writer per
checkout).

## 5. Lane A: every read surface

Rule: a function that turns a speaker into a NAME or a PROFILE LINK for anyone to see (response,
index, export, prompt, roster, filter) passes the file's decision. Per-file tasks resolve it once with
`privacy.gates.voiceprints_allowed(file_id, db)` (fresh read, fail closed = hidden); request code uses
one `PrivacyView`.

5.1 **Chunk index.** `resolve_chunk_speaker_name(speaker, *, voiceprint_names: bool)` (required kw)
and the `"profile_id": ...` at `index_transcript_search_task` (~`:259`) via `visible_profile_id`.
`reindex_task._resolve_reindex_speaker_name(row, *, voiceprint_names: bool)`; its `spk_rows` query
in `_extract_file_metadata` selects `Speaker.identity_source`, and the segment dicts' `profile_id`
goes through `visible_profile_id`.

5.2 **LLM prompts, digests, facts.** `transcript_builders.get_speaker_name(segment, *,
voiceprint_names: bool = True)`, threaded through `build_full_transcript`, `build_speaker_segments`,
`compute_speaker_stats`, `build_transcript_and_stats` (same kw). Callers pass the file decision:
`summarization._load_summarization_inputs` and `fingerprint_transcript_segments` (a flip changes the
fingerprint, which correctly marks the summary stale), `speaker_identification_task` (~`:521-522`),
`ingest_artifacts/service.load_ordered_segments` / `generate_file_artifacts`.

5.3 **Exports and captions.** `transcript_export_turns.speaker_namer(speakers, default_label, *,
voiceprint_names: bool)`; `transcript_export_service.build_export_content` (~`:83`) passes it;
`captions/source.speaker_labels_for(..., voiceprint_names: bool)` and `load_caption_segments` resolve
the file decision. These run in workers that commit: use the label kwarg, never
`redact_speaker_rows`, on these paths.

5.4 **Rename propagation.** `rename_propagation_task._current_speaker_name` selects
`Speaker.identity_source, Speaker.media_file_id` and passes the decision. `propagate_speaker_rename`:
at run time, if the file's voiceprints are off, call `queue_transcript_reindex` for that file and
return `{"status": "reindex_queued"}` instead of `update_by_query` (the indexed label may be the
hidden one, so an `old_names` filter computed without policy would match nothing). The tracker call
sites stay as they are (allowlisted in A1).

5.5 **Chat roster.** `build_candidate_roster` selects `identity_source` and `media_file_id`, adds
`not_(name_hidden)` from `machine_identity_clauses` to the `display_name` candidate column and drops
voiceprint `suggested_name` matches for off files; `_group_speaker_rows` passes
`voiceprint_names` per row (`PrivacyView`).

5.6 **Gallery and collections.** `files/__init__.py` and `media_collections.py` list options add
`Speaker.identity_source`, `Speaker.media_file_id` to `load_only` and call
`hide_machine_identities(view, speakers)` after loading, before serializing.
`files/filtering.py` speaker filter: `visible_display_name_expr(name_hidden) == s` scoped to the
query's file ids. `files/crud.py` `_resolve_segment_speaker_name` reads rows already passed through
`redact_response_speakers`: confirm, and allowlist with that reason.

5.7 **Profiles.** `speaker_profiles.py` per-profile linked-speaker counts (~`:118-127`): a profile
whose scope is off counts only links with `identity_source` NOT in the machine set.

5.8 **formatting_service.** `format_speaker_name` / `format_transcript_segment`: every caller must
pass rows already redacted (file detail does via `redact_response_speakers`); if a caller does not,
add `voiceprint_names`. Record each call site in A1's allowlist with the upstream redaction named.

5.9 **Resync on a flip.** New `app/services/privacy/label_resync.py`:
- `request_label_resync(db, scope: ResyncScope)`; `ResyncScope(kind: "instance" | "organization" |
  "user", organization_id, user_id)`. Appends to `SystemSettings` key `privacy.label_resync.pending`
  (JSON list, deduped), and after the caller's commit dispatches `privacy.label_resync`.
- `affected_file_ids(scope)`: COMPLETED files in scope with a speaker where `identity_source IN
  ('voiceprint','voiceprint_link')` OR (`suggestion_source IN EMBEDDING_SUGGESTION_SOURCES` AND
  `confidence >= 0.75` AND empty `display_name`). User scope = files owned by that user (the user
  layer applies to all their scopes).
- `run_label_resync(db, *, reindex=queue_transcript_reindex)`: per pending scope, page by id (500),
  queue each file (debounced; `ALREADY_PENDING` counts as done), remove the scope when finished,
  return counts. Idempotent.
- Hooks: `policy_admin.update_instance_policy` (when its `changes` contain the voiceprint key),
  `write_user_switch` (when the value changed), `apply_org_policy_update` (when
  `voiceprint_matching_enabled` changed). Never on an unchanged value.
- `app/tasks/privacy_label_resync_task.py` (`privacy.label_resync`, CPU queue, listed in
  `core/celery.py` `include=` AND the route table; a missing include is silent, see `e397819f`).
- Safety net: `search_maintenance_task._run_search_maintenance` runs `run_label_resync` when pending
  is non-empty (covers a dispatch that never ran).
- Seam note in `docs/design/v060_downstream_changes.md`: a derived deployment whose ceiling resolver
  or consent hook changes its answer must call `request_label_resync`; core cannot observe it.

## 6. Lane B: writes, purge, and the reads inside its files

6.1 Wire every D2 site. `join_cluster_identity` gains the required `source` kw; update both callers.

6.2 **Purge** (`purge.purge_voiceprints_postgres`), new steps BEFORE the suggestion step:
```python
reverted = UPDATE speaker SET display_name=NULL, profile_id=NULL, verified=FALSE,
    suggested_name=NULL, suggestion_source=NULL, confidence=NULL, identity_source=NULL
  WHERE <scope> AND identity_source = 'voiceprint' RETURNING id
unlinked = UPDATE speaker SET profile_id=NULL, identity_source=NULL
  WHERE <scope> AND identity_source = 'voiceprint_link' RETURNING id
```
Then for `reverted ∪ unlinked`: delete their `speaker_cluster_member` rows in promoted clusters, set
`speaker.cluster_id = NULL` where it points at a promoted cluster, recount `member_count` of the
touched promoted clusters, delete promoted clusters left with 0 members (the `speaker_cleanup` empty
cluster rule; their centroid docs go with the scope's OpenSearch delete). Append both id lists to
`cleared` so `refresh_cleared_speakers` recomputes status and queues the reindex. Counts:
`machine_identities_reverted`, `machine_links_removed`, `machine_cluster_members_removed`.
Update the module docstring's "What is deleted" and "Kept" lists. Never touches `human`/NULL rows.

6.3 **Occurrences.** `speaker_matching_service.find_speaker_occurrences`: when the profile's scope is
off (`self._voiceprints_allowed(profile.user_id, profile.organization_id)` is False), exclude
`Speaker.identity_source.in_(MACHINE_IDENTITY_SOURCES)`.

6.4 **Name directory.** `speakers.py` grouped query (`label_col`, `has_human_name`, the
`profile_id` filters and `func.min(Speaker.profile_id)`): use `visible_display_name_expr` /
`visible_profile_id_expr` from `machine_identity_clauses(view, <the accessible file subquery>)`.
The admin `/speakers/debug/cross-media-*` routes are matching replays for operators: leave them,
listed in A1/B2 allowlists with that reason.

6.5 **Discipline test** B2 (§7).

6.6 **Docs** (Lane B): `docs-site/docs/features/privacy-controls.md` "What is hidden" (names and
links a voiceprint made, everywhere they are shown; typed or confirmed names never), "Purge" (they
revert to unnamed), and an "Upgrading to v0.6.0" note: which pre-upgrade identifications are
recognised (D3 shape) and which are treated as a person's, the count query below, and that LLM
summaries already written are not rewritten. `CHANGELOG.md` `[Unreleased]` Fixed + Changed entries.
`backend/app/services/CLAUDE.md`: in "A speaker's label comes from a human or a voiceprint" add the
`identity_source` rule and the helper module; in the Privacy bullet name the purge revert.
```sql
SELECT count(*) FILTER (WHERE identity_source = 'voiceprint') AS recognised,
       count(*) FILTER (WHERE identity_source IS NULL AND verified IS TRUE
                        AND suggestion_source IN ('voice_match','profile_match')) AS unknown
FROM speaker;
```

## 7. Tests (each seen red before the change)

Red-check per root CLAUDE.md, never in the shared checkout:
`RED="$(mktemp -d -t ot-redcheck.XXXXXX)" && git archive <base> | tar -x -C "$RED"`, copy only the new
test files in. `<base>` = `f63b4043` for Phase-0 tests (red by import/attribute error is accepted
ONLY for these new-API tests); `<base>` = the Phase-0 commit for every lane test, so they fail by
ASSERTION (the column and helpers exist, the consumers/writers do not use them yet). Where a test is
a control that HEAD already satisfies, the red is shown by the named mutation on the lane's own tree
(revert that one line in an `ot-redcheck` copy). Writers' OpenSearch/kNN seams are monkeypatched;
assertions are on Postgres rows or produced bytes. Run `python3 ../scripts/audit-tests.py tests` to 0.

**Phase 0**
- T0.1 `tests/unit/test_v449_migration_consistency.py`: `test_v449_revision_chain` (parent per D4,
  single head); `test_vendor_neutral`; `test_column_is_nullable_varchar16`;
  `test_check_rejects_an_unknown_value` (IntegrityError on `'bogus'`) with
  `test_check_accepts_every_documented_value` as control; `test_backfill_stamps_only_the_profile_auto_accept_shape`
  (seed: the D3 shape -> `voiceprint`; same with `voice_match` -> NULL; `suggested_name` NULL ->
  NULL; display != profile name -> NULL; existing `human` -> unchanged);
  `test_backfill_is_idempotent` (second replay updates 0 rows); `test_detected_at_or_after_v449`
  (`assert_detected_at_or_after`, never `== REVISION`).
- T0.2 `tests/unit/test_canonical_speaker_label.py` (+): `test_a_voiceprint_name_falls_back_to_the_diarizer_label_when_off`
  (control: same row, flag True -> "Alice"); `test_a_voiceprint_link_keeps_its_name_when_off`;
  `test_a_human_or_unknown_name_is_never_hidden` (parametrize `human`, None);
  `test_a_voice_suggestion_is_not_promoted_when_off`; `test_for_row_without_identity_source_raises_when_off`
  (control: same row with flag True returns a label); `test_visible_profile_id_matrix`.
- T0.3 `tests/unit/test_privacy_redact_view.py` (+):
  `test_a_voiceprint_made_identity_is_hidden_in_memory_and_kept_in_the_database` (off org: name,
  profile, verified, suggestion and `resolved_display_name` hidden; `db_session.expire(row)` then the
  DB still has them; control in an on org unchanged);
  `test_a_voiceprint_link_hides_only_the_link`; `test_a_typed_name_is_never_hidden` (human, None);
  `test_machine_identity_clauses_select_exactly_the_off_scope_rows` (personal off, org off, org on,
  human row: only the two off machine rows match; the personal row is the NULL-tuple guard; mutation
  red: swap the `or_` for `tuple_().in_()`).

**Lane B**
- B1 `tests/unit/test_speaker_identity_provenance_writes.py`:
  `test_an_auto_accepted_name_is_recorded_as_a_voiceprint` (real `_handle_speaker_match`, OpenSearch
  seams patched); `test_an_auto_accepted_name_is_hidden_once_voiceprints_are_off` (same, then org off,
  `redact_speaker_rows` -> `display_name is None`); `test_auto_accept_onto_a_typed_name_records_only_the_link`;
  `test_propagation_records_a_link_and_never_downgrades_a_voiceprint`;
  `test_retroactive_auto_apply_is_a_voiceprint`; `test_an_upload_cluster_join_is_a_voiceprint_and_a_merge_join_is_human`;
  `test_unlink_rule` (link-only -> NULL with name kept; emptied name -> NULL).
  `test_each_human_action_turns_a_voiceprint_identity_into_a_human_one`, parametrized over: PUT
  display_name, PUT `profile_action`, verify accept, assign-profile, `_create_new_speaker_profile`,
  batch-verify accept/name/assign, promote; each also asserts the name survives `redact_speaker_rows`
  while off. Red per row: delete that site's `record_human_identity` in an `ot-redcheck` copy.
- B2 `tests/unit/test_speaker_identity_source_discipline.py`: AST over `backend/app`; every function
  that assigns `.display_name` / `.profile_id` (attribute, `setattr(speaker, field, ...)`, or a
  `.values(display_name=|profile_id=)` on `Speaker`) must call a helper from
  `utils/speaker_identity_source` or set `identity_source`, or be in `_ALLOWED`
  (`<file>::<function>: reason`, e.g. `speaker_profile_rename`, `speaker_cleanup.unlink_from_profile`
  uses `after_unlink`, non-Speaker objects). `test_detector_fires_on_an_unmarked_assignment` and
  `test_detector_stays_clean_on_a_marked_one` (inline source fixtures). Red on the Phase-0 base: the
  writers are unmarked.
- B3 `tests/api/test_privacy_purge.py` (+):
  `test_a_purge_removes_an_auto_accepted_name_and_link` (seed through `_handle_speaker_match`, org
  off, purge: `display_name`, `profile_id` NULL, `verified` False, counts, the reindex callback got the
  file, `resolved_display_name` is the diarizer label);
  `test_a_purge_keeps_a_typed_name_and_removes_only_a_machine_link`;
  `test_a_purge_never_touches_a_human_or_unknown_identity` (mutation red: drop the
  `identity_source =` condition); `test_a_purge_in_one_organization_leaves_another_organizations_machine_identities`;
  `test_a_purge_takes_machine_members_out_of_a_promoted_cluster` (member row gone, `member_count`
  recounted, a cluster left empty deleted, a human member kept).
- B4 `tests/api/test_speaker_profile_occurrences_privacy.py`:
  `test_occurrences_omit_machine_links_while_off` + `test_occurrences_include_them_while_on`.
- B5 `tests/api/test_speaker_name_directory_privacy.py`: `test_a_hidden_machine_name_is_not_offered`
  (off: "Alice" absent, the row appears under its diarizer label only with `include_unnamed`), on
  control, and a typed "Bob" offered while off.
- B6 the v447 detection widening, if 0.3 needed it.

**Lane A**
- A1 `tests/unit/test_speaker_label_call_site_discipline.py`: AST; every call of
  `canonical_speaker_label`, `canonical_speaker_label_for_row`, `get_speaker_name`,
  `resolve_chunk_speaker_name`, `_resolve_reindex_speaker_name`, `speaker_namer`, `speaker_labels_for`,
  `build_full_transcript`, `build_speaker_segments`, `compute_speaker_stats`,
  `build_transcript_and_stats` passes `voiceprint_names=` or is in `_ALLOWED` with a reason (rename
  tracker before/after captures; `speaker_status_service` = stored, policy-free status;
  `chat/aggregation_service` labels names it was given; `formatting_service` and `files/crud` sites
  fed redacted rows). Must-fire and must-stay-clean fixtures.
- A2 `tests/unit/test_chunk_index_label_privacy.py`: `test_index_writes_the_diarizer_label_and_no_profile_for_a_hidden_identity`
  (run `index_transcript_search_task` for an off file with the bulk writer captured) + on control;
  same for `_extract_file_metadata`.
- A3 `tests/unit/test_rename_propagation_privacy.py`: `test_a_rename_in_an_off_scope_queues_a_full_reindex`
  (no `update_by_query` call) + `test_a_rename_in_an_on_scope_rewrites_in_place`.
- A4 `tests/unit/test_export_caption_label_privacy.py`: `txt`, `srt`, `md` exports and
  `load_caption_segments` for an off file contain no "Alice" and contain the diarizer label; on
  control contains "Alice"; a typed "Bob" appears in both.
- A5 `tests/unit/test_llm_prompt_label_privacy.py`: `_load_summarization_inputs` and
  `build_full_transcript` for an off file have no "Alice"; on control.
- A6 `tests/unit/test_ingest_facts_label_privacy.py`: facts roster for an off file has no "Alice".
- A7 `tests/unit/test_chat_roster_privacy.py`: "Alice" does not resolve in an off file; resolves in
  an on file; "Bob" (human) resolves in the off file.
- A8 `tests/api/test_gallery_speaker_privacy.py`: gallery list chips, collection list, and
  `?speaker=Alice` filter return no "Alice" / no match for an off scope; controls on; profile counts
  exclude machine links while off.
- A9 `tests/unit/test_privacy_label_resync.py`: `test_turning_an_org_off_queues_exactly_its_affected_files`
  (other org's file and a human-only file not queued); `test_turning_it_back_on_queues_them_again`;
  `test_an_unchanged_patch_queues_nothing`; `test_a_pending_scope_survives_a_failed_dispatch_and_the_maintenance_arm_finishes_it`;
  `test_the_task_is_registered` (name in the Celery app's task registry, route = cpu).
- A10 `tests/api/test_voiceprint_identity_round_trip.py`:
  `test_hide_then_restore_leaves_the_row_byte_identical` (auto-accepted speaker; instance switch off:
  `GET /api/files/{uuid}` speakers, `GET /api/speakers`, a txt export and `resolve_chunk_speaker_name`
  all hide; switch on: all show; the Postgres row's identity columns equal before, during and after).
- A11 extend `tests/unit/test_speaker_label_provenance.py` if its literal scan needs the new values
  classified (it scans `suggestion_source` literals; `identity_source` literals get the same
  "every value is classified" check here).

## 8. Order of work

1. Phase 0 (§3) in `lane-pfb`, commit, report the sha.
2. `lane-pfa`: `git merge --no-ff <phase0-sha>`; then Lane A (§5) and Lane B (§6) in parallel.
3. Each lane, before reporting: its tests red-then-green; `python3 ../scripts/audit-tests.py tests` = 0;
   `scripts/safe-precommit.sh run --all-files` and `--hook-stage pre-push`; commit in its own worktree.
   Lane A: `docs(privacy)`/`fix(privacy)` commits; Lane B likewise. One `Closes`-free reference to #1280.
4. Integration (orchestrator, after both report): merge B then A into `feat/privacy-controls-1280`
   with `--no-ff`; resolve D4's parent if wave-1 landed first; `alembic-head.py` single head;
   run §9.

## 9. Verification

1. On an isolated stack holding v449 (`./opentr.sh start dev --fresh pprov`, export every `*_PORT`):
   the v449/v447 consistency suites, all tests in §7, `./scripts/run-dev-tests.sh --backend-only`.
2. Live, `--fresh pprov`: upload two clips of one voice; name the first (typed, creates a profile);
   the second auto-accepts. `SELECT identity_source FROM speaker` shows `human` / `voiceprint`.
   Turn the instance switch off (`PUT /api/admin/privacy-policy`): the second file's speaker shows its
   diarizer label in the file page, the speakers page, a txt and srt export and the captions; after
   the resync task, `/api/search?q=<name>` with a speaker filter returns only the first file; the chat
   roster ("what did <name> say") resolves only the first file. The first file still shows the typed
   name everywhere. Open the file page in a browser (light and dark) to confirm.
3. Turn it back on: everything reappears; the row is unchanged.
4. Off again, `POST /api/admin/privacy-policy/purge {"scope":"personal","targets":["voiceprints"],
   "confirm":"PURGE"}`: the second speaker is unnamed and unlinked; the first keeps name and link;
   counts match.
5. `alembic downgrade v447` then `upgrade head` on the fresh stack: idempotent, no error.

## 10. Risks and rollback

1. **Misclassification at upgrade (D3).** Residual: `v439`-stamped legacy rows and an identical
   human re-assignment are recognised as machine and would be purged. Everything else is
   under-recognised (safe). Mitigation: narrow predicate, the count query, the upgrade note, owner Q1.
2. **A missed write site** leaves NULL (= human): the leak silently persists for that path. B2 makes
   an unmarked identity write a test failure; A1 does the same for an unparametrised label read.
3. **Hiding persisted by accident.** A path that redacts and then `setattr`s status and commits
   would store the hidden state. Mitigation: `set_committed_value` only, status recomputed on the
   redacted row only in the response helpers, workers use the label kwarg (§5.3); T0.3 re-reads the
   row from the database.
4. **Index lag and load.** Search/chat labels converge only when the resync reindex has run; an
   instance-wide flip on a large library queues one debounced reindex per affected file on the CPU
   queue. API, exports, captions and prompts are immediate.
5. **Migration collision** with wave-1's v448 (D4). `alembic-head.py` after every merge.
6. **Query cost** of `machine_identity_clauses` on list pages: one DISTINCT-scope query per request,
   bounded by the accessible file subquery; `(false(), false())` short-circuits when nothing is off.

Rollback: revert the two lane merges (code ignores the column; nothing reads it). If the schema must
go: `alembic downgrade v447_add_content_hold` (or v448) drops `ck_speaker_identity_source` and the
column; names are untouched, the markers are lost, and a purge already run stays irreversible (as the
purge dialog states). A reverted build behaves exactly as `f63b4043`.

## 11. Owner questions

- **Q1 (recommended: yes).** Backfill the D3 `profile_match` auto-accept shape as `voiceprint`,
  accepting the rare residue in §10.1? The alternative is no backfill: every identification made
  before the upgrade counts as a person's and is never hidden or purged.
- **Q2 (recommended: human).** Promoting a cluster to a named profile: are its members' names a
  person's confirmation (kept by a purge, as promoted clusters already are) or voiceprint-derived?
- **Q3 (recommended: accept, disclosed).** LLM-written summaries and topics that already name a
  hidden speaker are not rewritten or hidden; regenerating them while off removes the name.
