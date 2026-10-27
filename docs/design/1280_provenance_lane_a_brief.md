# #1280 voice-match provenance: Lane A brief (read surfaces + label resync)

Self-contained brief for one implementer. Parent plan: `docs/design/1280_voice_match_provenance_plan.md`
(read its "Reconciliation with merged fixes" section first, then §2 Decisions, §5, §7). Where this
brief and the plan's §3-§5 disagree, this brief wins. Lane B (writes, purge, docs) runs in
parallel in another worktree; do not edit its files (list at the end).

## 0. Ground rules

- Base: `feat/privacy-provenance-1280` (Phase 0 code = `ba66dfcf3` + `9d57b6aed`; use `9d57b6aed`, or the
  branch tip, as `<phase0-sha>` below; the branch holds
  `backend/alembic/versions/v449_add_speaker_identity_source.py`). Work in your own worktree
  (`.claude/worktrees/<name>`), on your own branch, as the ONLY writer in it. Do not push unless
  your coordinator says so.
- Root `CLAUDE.md` rules apply: never `pkill`/`kill`, no GPU or Docker volume changes, never read
  `.env`, `./opentr.sh` for stack operations, `scripts/safe-precommit.sh` (both tiers) before
  committing, never `--no-verify`, never silence a linter, commit with an explicit pathspec
  (`git commit -- <files>`), conventional commits (`fix(privacy): ...`, `feat(privacy): ...`,
  `test(privacy): ...`, `docs(privacy): ...`), neutral wording, reference `#1280` without a
  closing keyword.
- Python: `/mnt/nvm/repos/transcribe-app/backend/venv/bin/python` from your worktree's `backend/`.

## 1. Owner decisions (final, 2026-10-27)

- Build voice-match provenance fully for 0.6.0: every speaker identity records where it came
  from; no shortcuts.
- Q1: the legacy `profile_match` auto-accept shape is backfilled as `voiceprint` by v449 and the
  count is recorded (`system_settings` key `privacy.identity_source_backfill`). Lane B documents it.
- Q2: members of a promoted cluster count as HUMAN.
- Q3: LLM summaries and topics already generated are NOT rewritten; disclose it. (The INDEX copy
  of a summary is a different matter: see A.4.)
- Every label resync caused by a switch flip writes an AUDIT event (actor, scope, file counts
  selected / queued / already pending / failed). It is measured, not assumed.

## 2. The Phase 0 contract you build on (already merged into your base)

- `speaker.identity_source`: `voiceprint` (machine wrote name + link), `voiceprint_link` (machine
  added only the link), `human`, NULL (unknown, read as human). Module
  `app/utils/speaker_identity_source.py`: `VOICEPRINT`, `VOICEPRINT_LINK`, `HUMAN`,
  `IDENTITY_SOURCES`, `MACHINE_IDENTITY_SOURCES`, `hides_name(source, enabled)`,
  `hides_link(source, enabled)`, `visible_profile_id(profile_id, source, voiceprint_names)`.
- `app/utils/speaker_labels.py`:
  `canonical_speaker_label(name, *, display_name, suggested_name, confidence, suggestion_source,
  verified=None, identity_source=None, voiceprint_names=True)` (pure) and
  `canonical_speaker_label_for_row(row, db=None, *, voiceprint_names: bool | None = None)`:
  `None` = ask the policy for the row's FILE (through `db`: a session or a `PrivacyView`, else the
  ORM row's own session), `True` = policy-free, `False` = withhold. A columns-only row that did
  not select `Speaker.identity_source`, names somebody, and whose file is off RAISES `TypeError`.
  Every columns-only query feeding a label MUST select `Speaker.identity_source`,
  `Speaker.verified`, `Speaker.media_file_id`, `Speaker.suggestion_source`.
- `app/services/privacy/redact_view.py`: `PrivacyView` (memoised per unit of work;
  `voiceprints(file_id)`, `voiceprints_off_file_ids(ids)`, `label_for_row(row)`),
  `voiceprints_off_for_row(row, db)` (fails closed), `hides_voiceprint_suggestion(row)`,
  `hide_machine_identities(view, rows) -> changed` (`set_committed_value` only; safe on
  `load_only` rows that load `identity_source, media_file_id, display_name, profile_id,
  verified`), `redact_speaker_rows(view, rows)` (identities, suggestions, gender; recomputes
  status of changed rows), `machine_identity_clauses(view, file_ids_select) -> (name_hidden,
  link_hidden)` and `visible_display_name_expr(name_hidden)` / `visible_profile_id_expr(link_hidden)`.
- `SpeakerStatusService.compute_speaker_status(speaker, *, voiceprint_names=None)`; the PERSISTED
  status is policy-free (`update_speaker_status_in_db` passes `True`). A response must redact first
  and then compute status (see `files/segments.py` ~:100-104 for the canonical order).

## 3. Files you own

`app/tasks/search_indexing_task.py`, `app/tasks/reindex_task.py`, `app/tasks/rename_propagation_task.py`,
`app/utils/transcript_builders.py`, `app/tasks/summarization.py`,
`app/tasks/speaker_identification_task.py` (prompt-build calls only), `app/services/ingest_artifacts/service.py`,
`app/services/ingest_artifacts/index_mapping.py` (summary documents, A.4),
`app/services/transcript_export_service.py`, `app/services/transcript_export_turns.py`,
`app/services/captions/source.py`, `app/services/chat/speaker_resolver.py`,
`app/api/endpoints/files/__init__.py`, `files/filtering.py`, `files/crud.py`, `files/segments.py`,
`app/api/endpoints/transcript_segments.py` (response shaping only; its audit scoping at ~:124 belongs
to another lane), `app/api/endpoints/media_collections.py`, `app/services/formatting_service.py`,
`app/api/endpoints/speaker_profiles.py` (counts only), `app/services/privacy/label_reindex.py`,
new `app/tasks/privacy_label_resync_task.py`, `app/core/celery.py`, `app/services/privacy/policy_admin.py`,
`app/tasks/search_maintenance_task.py`, `app/services/privacy/redact_view.py` (A.7 only),
`app/services/privacy/policy.py` (A.8 only), `app/auth/audit.py` (one new event type),
`backend/app/services/search/CLAUDE.md`, `backend/app/api/CLAUDE.md`, `docs/design/v060_downstream_changes.md`.
NOT yours (another lane): `speaker_profile_rename.py`, `transcript_segments.py:124` audit scoping,
`uuid_helpers.py:552`, `speaker_identification_task._get_known_speakers`.

## 4. Work items

**A.1 One decision per file, threaded (performance + determinism).** The merged
`for_row(row)` with no `db` builds a fresh `PrivacyView` per call, so a 2,000-segment file with a
voiceprint speaker costs a policy resolution per segment. Per-file tasks resolve the decision once
(`privacy.gates.voiceprints_allowed(file_id, db)` or `PrivacyView(db).voiceprints(file_id).enabled`,
fail closed = hidden) and pass `voiceprint_names=<bool>`; request code shares one `PrivacyView`.
- `search_indexing_task.resolve_chunk_speaker_name(speaker, *, voiceprint_names: bool)` (required
  kw) and the chunk dict's `"profile_id"` (~:254) via `visible_profile_id(speaker.profile_id,
  speaker.identity_source, voiceprint_names)`.
- `reindex_task._resolve_reindex_speaker_name(row, *, voiceprint_names: bool)`;
  `_extract_file_metadata`'s `profile_map` via `visible_profile_id` (`identity_source` is already
  selected).
- `transcript_builders.get_speaker_name(segment, *, voiceprint_names: bool | None = None)` threaded
  through `build_full_transcript`, `build_speaker_segments`, `compute_speaker_stats`,
  `build_transcript_and_stats`. Callers pass the file decision: `summarization._load_summarization_inputs`
  and `fingerprint_transcript_segments` (a flip changes the fingerprint: correct, the summary goes
  stale), `speaker_identification_task` prompt building (~:521), `ingest_artifacts/service.
  load_ordered_segments` / `generate_file_artifacts`.
- `transcript_export_turns.speaker_namer(..., *, voiceprint_names: bool)`,
  `transcript_export_service.build_export_content` (~:83), `captions/source.speaker_labels_for` /
  `load_caption_segments`. Workers that commit use the kwarg, never `redact_speaker_rows`.

**A.2 Rename propagation while off.** `rename_propagation_task.propagate_speaker_rename`: if the
file's voiceprints are off at run time, call `queue_transcript_reindex` for the file and return
`{"status": "reindex_queued"}` instead of `update_by_query` (the index may hold the hidden label,
so an `old_names` filter computed without policy matches nothing). Tracker call sites unchanged.
Note for every tracker you touch: the before/after labels use the policy-aware `for_row`, so the
writer (Lane B) sets `identity_source` BEFORE the "after" label is computed.

**A.3 Chat roster.** `build_candidate_roster`: the SQL candidate match on `display_name` gets
`not_(name_hidden)` from `machine_identity_clauses(view, <accessible file ids>)`, and voiceprint
`suggested_name` candidates for off files are dropped; `_group_speaker_rows` keeps the shared view.
Also pass the PROFILE link through `visible_profile_id` where the roster exposes `profile_id`.

**A.4 Summary plane in the index (audit item A-b).** `label_reindex` (and every reindex) rebuilds
the summary documents in `transcript_chunks` from `summary_data` verbatim
(`ingest_artifacts/index_mapping.build_summary_documents` ~:298), re-writing a hidden name into
the index. When a file has a hidden machine identity, the INDEX copy masks that name: word-boundary,
case-insensitive replacement of each hidden `display_name` (and hidden voiceprint suggestion) with
that speaker's diarizer label, only in what is indexed. The stored `summary_data` stays as written
(owner decision Q3, disclosed by Lane B's docs; say so in `search/CLAUDE.md`). Test: an off file's
summary documents do not contain "Alice", contain `SPEAKER_00`; on control contains "Alice"; a
human "Bob" survives.

**A.5 Gallery, collections, filters, profile counts.** `files/__init__.py` and
`media_collections.py` list options: add `Speaker.identity_source`, `Speaker.media_file_id` to
`load_only` and call `hide_machine_identities(view, speakers)` before serialising.
`files/filtering.py` speaker filter: `visible_display_name_expr(name_hidden) == s` scoped to the
query's file ids. `speaker_profiles.py` per-profile linked-speaker counts (~:118-127): a profile whose
scope is off counts only links whose `identity_source` is NOT machine.
`files/crud.py._resolve_segment_speaker_name` and `formatting_service` read rows already redacted:
confirm each caller and record it in the A1 allowlist with the upstream redaction named.

**A.6 Segment edit responses (audit item A-a).** `PUT /files/{uuid}/transcript/segments/{seg}`
responses (`transcript_segments.py` ~:358-376 and `files/crud.py` ~:1337-1352) must mirror
`files/segments.py` ~:100-104: `redact_response_speakers` (or `redact_speaker_rows`) THEN
`SpeakerStatusService.add_computed_status`, so no stored `resolved_display_name` / `status_text`
leaks a hidden name. Test with an off file and a voiceprint speaker; on control.

**A.7 `PrivacyView.preload_files` isolation (audit item A-c).** Its read (redact_view ~:87-95) runs
inside the caller's transaction; a failure aborts that transaction. Wrap it like
`policy._isolated` (a SAVEPOINT; on error roll back to it, deny, and leave the caller's
transaction usable). Test: force the query to fail, then prove the same session still executes a
query and the files read as off.

**A.8 Per-file consent (audit item A-e).** `policy.voiceprint_write_allowed` (~:500) passes
`media_file_id=None` to the consent hooks and `PrivacyView` memoises per (owner, organization).
Either pass the file through (memo key gains the file id when hooks are registered) or document,
in code and in `docs/design/v060_downstream_changes.md`, that consent hooks are per-subject
(owner + organization), never per-file. Pick one, state why, test it (a hook that refuses one
file of a scope: either that file alone is off, or the documented per-subject contract holds).

**A.9 Durable, audited label resync (§5.9 on the EXISTING `label_reindex.py`; audit item A-d).**
Keep one module (`services/privacy/label_reindex.py`); do not add a second resync module.
- Affected files: COMPLETED files in scope with a speaker where `identity_source IN
  ('voiceprint','voiceprint_link')` OR (`suggestion_source IN EMBEDDING_SUGGESTION_SOURCES` AND
  `confidence >= 0.75` AND empty `display_name` AND `verified IS NOT TRUE`). User scope = files
  that user owns (the user layer covers all their scopes).
- `request_label_resync(db, scope)`: append the scope to a durable pending record
  (`SystemSettings` key `privacy.label_resync.pending`, JSON list, deduped), commit, dispatch
  `privacy.label_resync` (CPU queue, registered in `core/celery.py` `include=` AND the route
  table: a missing include is silent). The three hooks in `policy_admin` call it instead of the
  current synchronous loop, only when the value actually changed.
- `run_label_resync(db, *, reindex=queue_transcript_reindex)`: per pending scope, page by id (500),
  queue each file (+ `dispatch_file_facts`), `ALREADY_PENDING` counts as done, a FAILED queue keeps
  the scope pending (retryable), remove the scope only when every file is queued. Idempotent.
- Safety net: `search_maintenance_task._run_search_maintenance` runs `run_label_resync` when the
  pending record is non-empty, AND detects drift the hooks cannot see (a derived deployment's
  ceiling resolver or consent hook changing its answer): compare, for files with a machine
  identity or a voiceprint suggestion, the label set the policy implies now with what was last
  indexed (record the decision used per file, e.g. a `privacy.label_resync.indexed` marker, or
  query the chunk index's distinct `speaker` values for those files) and queue the differing
  files. Document the seam in `docs/design/v060_downstream_changes.md`.
- AUDIT: a new `AuditEventType.PRIVACY_LABEL_RESYNC = "privacy.labels.resync"` written once when a
  resync is requested (actor user id/username, scope kind + `organization_id` / `user_id`, the
  switch and its from/to value) and once when a scope finishes (counts: `files_selected`,
  `files_queued`, `files_already_pending`, `files_failed`, `pages`). Add it wherever the existing
  privacy events are listed (`tests/unit/test_org_audit_privacy_events_labelled.py`, the docs
  "Audit events" table is Lane B's file: hand Lane B the row text, or list it for the integrator).

**A.10 Fail-closed coverage (audit item A-f).** Phase 0 already pins
`voiceprints_off_for_row` (no session, error, missing `verified`). Add the same for any new
decision point you introduce (A.1 per-file resolution: an exception hides).

## 5. Tests (each seen RED before the change, then green)

Red-check method (root `CLAUDE.md`): `RED="$(mktemp -d -t ot-redcheck.XXXXXX)" && git archive
<phase0-sha> | tar -x -C "$RED"`, copy ONLY your new/changed test files in, run them there; they
must fail by ASSERTION (the column and helpers exist; the consumer does not use them yet). A
control that the base already satisfies is shown red by a named one-line mutation applied in an
`ot-redcheck` copy of YOUR tree (record the mutation and the failing assertion). Never revert files
in place. Remove the copies afterwards.

- A1 `tests/unit/test_speaker_label_call_site_discipline.py`: AST over `backend/app`; every call of
  `canonical_speaker_label`, `canonical_speaker_label_for_row`, `get_speaker_name`,
  `resolve_chunk_speaker_name`, `_resolve_reindex_speaker_name`, `speaker_namer`,
  `speaker_labels_for`, `build_full_transcript`, `build_speaker_segments`, `compute_speaker_stats`,
  `build_transcript_and_stats` passes `voiceprint_names=` or a `PrivacyView`/session as `db`, or is
  in `_ALLOWED` (`<file>::<function>: reason`): tracker before/after captures,
  `speaker_status_service` (status is computed on redacted rows / persisted policy-free),
  `chat/aggregation_service` (labels names it was given), `formatting_service` / `files/crud` (fed
  redacted rows, name the redaction). Plus: every `db.query(Speaker.<col>, ...)` whose rows reach a
  label function selects `Speaker.identity_source`. Must-fire and must-stay-clean inline fixtures.
- A2 `tests/unit/test_chunk_index_label_privacy.py`: `index_transcript_search_task` for an off file
  with the bulk writer captured writes the diarizer label and NO `profile_id` for a voiceprint
  identity; a `voiceprint_link` keeps the name, drops the link; human keeps both; on control.
  Same for `_extract_file_metadata`.
- A3 `tests/unit/test_rename_propagation_privacy.py`: off scope queues a reindex, no
  `update_by_query`; on scope rewrites in place.
- A4 `tests/unit/test_export_caption_label_privacy.py`: txt/srt/md exports and
  `load_caption_segments` for an off file: no "Alice", diarizer label present; on control "Alice";
  a typed "Bob" in both. Use a speaker with `identity_source='voiceprint'`, `verified=True`,
  `display_name='Alice'` (the Phase 0 contract makes this hidden; today only suggestions were).
- A5 `tests/unit/test_llm_prompt_label_privacy.py`: `_load_summarization_inputs` and
  `build_full_transcript` for an off file have no "Alice"; on control; and the fingerprint changes
  across a flip.
- A6 `tests/unit/test_ingest_facts_label_privacy.py`: facts roster / digest for an off file has no "Alice".
- A6b `tests/unit/test_summary_index_masking.py` (A.4).
- A7 `tests/unit/test_chat_roster_privacy.py`: "Alice" (voiceprint identity) does not resolve in an
  off file; resolves in an on file; "Bob" (human) resolves in the off file.
- A8 `tests/api/test_gallery_speaker_privacy.py`: gallery chips, collection list, `?speaker=Alice`
  filter, profile linked-speaker counts: hidden / excluded while off; controls on.
- A8b `tests/api/test_segment_edit_response_privacy.py` (A.6).
- A9 `tests/unit/test_privacy_label_resync.py`: turning an org off queues exactly its affected files
  (another org's file and a human-only file not queued); back on queues them again; an unchanged
  patch queues nothing; a pending scope survives a failed dispatch and the maintenance arm finishes
  it; drift detection queues a file whose indexed label disagrees with the policy; the task is
  registered (name in the Celery registry, route = cpu); the audit events carry the exact counts
  (assert on the persisted audit record, not a mock call).
- A10 `tests/api/test_voiceprint_identity_round_trip.py`: auto-accept-shaped speaker
  (`identity_source='voiceprint'`); instance switch off: `GET /api/files/{uuid}`, `GET
  /api/speakers`, a txt export and `resolve_chunk_speaker_name` all hide; switch on: all show; the
  Postgres row's identity columns are byte-identical before, during and after.
- A11 extend `tests/unit/test_speaker_label_provenance.py` if its literal scan needs the
  `identity_source` values classified (every literal written must be in `IDENTITY_SOURCES`).
- A.7 / A.8 / A.10 tests as described in their items.
- Regression: `tests/api/test_voice_match_label_policy.py`, `tests/api/test_privacy_switch_relabels_index.py`,
  `tests/unit/test_privacy_*.py`, `tests/unit/test_canonical_speaker_label.py`,
  `tests/unit/test_speaker_identity_source.py`, `tests/unit/test_*migration*consistency*.py` stay green.

## 6. Test database (must hold v449)

The shared dev stack (`localhost:5176`) runs the main checkout's schema and does NOT have v449.
Either (a) an isolated stack, if your coordinator allows Docker: `./opentr.sh start dev --fresh
<name> --port-offset <N>` and export every `*_PORT`, or (b) no Docker at all (how Phase 0 ran):

```bash
python3.12 -m venv /tmp/ot-pg-venv && /tmp/ot-pg-venv/bin/pip install -q pgserver
B=/tmp/ot-pg-venv/lib/python3.12/site-packages/pgserver/pginstall/bin
E=$B/../share/postgresql/extension   # pgserver lacks uuid-ossp; v010 only needs the extension to exist
printf "comment='test shim'\ndefault_version='1.1'\nrelocatable=true\n" > $E/uuid-ossp.control
echo "CREATE FUNCTION uuid_generate_v4() RETURNS uuid AS 'SELECT gen_random_uuid()' LANGUAGE SQL;" > $E/uuid-ossp--1.1.sql
D=$(mktemp -d -t ot-pgdata.XXXXXX); $B/initdb -D $D/data -U postgres --auth=trust -E UTF8
$B/pg_ctl -D $D/data -o "-p <free port> -k $D -c listen_addresses=127.0.0.1" -l $D/log start
$B/psql -h 127.0.0.1 -p <port> -U postgres -c "CREATE DATABASE transcribe_test"
export DATA_DIR=/tmp/<you>/data MODELS_DIR=/tmp/<you>/models TEMP_DIR=/tmp/<you>/temp \
  POSTGRES_HOST=127.0.0.1 POSTGRES_PORT=<port> POSTGRES_USER=postgres POSTGRES_PASSWORD=x \
  POSTGRES_DB=transcribe_test SKIP_S3=True SKIP_OPENSEARCH=True
cd backend && $PY -m alembic upgrade head && $PY -m pytest ...
# finish: $B/pg_ctl -D $D/data stop   (never kill)
```
Option (b) skips OpenSearch-backed tests (they report skips); say so in your report and run those
on a stack that has OpenSearch before claiming them.

## 7. Verification before reporting

1. All A-tests red-then-green with the evidence recorded (commit body or report).
2. `cd backend && python3 ../scripts/audit-tests.py tests` = 0 open findings.
3. `scripts/safe-precommit.sh run --all-files` and `scripts/safe-precommit.sh run --all-files
   --hook-stage pre-push`: both exit 0.
4. Report: commit shas, test counts from a junit file, red evidence per test, what was not run
   (OpenSearch-backed, live browser) and why.

## 8. Final-integration checklist (for the integrator, after Lanes A and B report)

- Merge B then A into the integration branch with `--no-ff`; resolve v449's parent if wave-1's v448
  is present (plan addendum, three one-line edits); `python3 scripts/release-tests/lib/alembic-head.py
  backend` prints exactly one head; `test_v449_migration_consistency` green.
- Regenerate generated artefacts the hooks check (privacy matrix / docs tables if a hook reports
  drift), `frontend` OpenAPI types if any schema changed (`PurgeStatus`), CHANGELOG `[Unreleased]`.
- 12-locale parity for any new UI string (`cd frontend && npm run check:i18n`; real translations,
  `ar` checked RTL); the purge dialog must show the measured per-store counts, the residual
  verification result and the audit reference.
- Full gate on a stack holding v449: `./scripts/run-dev-tests.sh --backend-only` (or the
  integration gate), then the live walk in plan §9 including a browser check (light + dark).
- Upgrade path: `alembic downgrade v447_add_content_hold` (or v448) then `upgrade head` on a fresh
  stack; the backfill record is re-created with the new count.

## Lane B owns (do not edit)

`app/services/speaker_matching_service.py`, `app/api/endpoints/speaker_update.py`,
`app/api/endpoints/speakers.py`, `app/services/speaker_cleanup.py`,
`app/services/speaker_clustering_service.py`, `app/services/privacy/purge.py`,
`app/tasks/privacy_purge_task.py`, `app/services/gdpr_erasure_service.py`,
`app/services/opensearch_service/client.py`, `app/schemas/privacy.py`, the purge UI and its i18n
keys, `docs-site/docs/features/privacy-controls.md`, `docs-site/docs/operations/upgrading-to-0-6-0.md`,
`CHANGELOG.md`, `backend/app/services/CLAUDE.md`.
