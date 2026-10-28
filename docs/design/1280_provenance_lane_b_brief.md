# #1280 voice-match provenance: Lane B brief (writes, speaker semantics, audited purge, docs)

Self-contained brief for one implementer. Parent plan: `docs/design/1280_voice_match_provenance_plan.md`
(read its "Reconciliation with merged fixes" section first, then §2 Decisions, §6, §7). Where this
brief and the plan's §3-§6 disagree, this brief wins. Lane A (read surfaces, label resync) runs in
parallel in another worktree; do not edit its files (list at the end).

## 0. Ground rules

- Base: `feat/privacy-provenance-1280` (Phase 0 code = `ba66dfcf3` + `9d57b6aed`; use `9d57b6aed`, or the
  branch tip, as `<phase0-sha>` below; it holds
  `backend/alembic/versions/v449_add_speaker_identity_source.py`). Own worktree
  (`.claude/worktrees/<name>`), own branch, ONLY writer in it. Do not push unless told.
- Root `CLAUDE.md` rules: never `pkill`/`kill`, no GPU or Docker volume changes, never read
  `.env`, `./opentr.sh` for stack operations, `scripts/safe-precommit.sh` (both tiers) before
  committing, never `--no-verify`, never silence a linter, `git commit -- <explicit files>`,
  conventional commits (`feat(privacy)`, `fix(privacy)`, `test(privacy)`, `docs(privacy)`),
  neutral wording, reference `#1280` without a closing keyword. Frontend: Svelte + TypeScript,
  `$lib` aliases, light/dark parity, all 12 locales translated (not copied English), `ar` RTL.
- Python: `/mnt/nvm/repos/transcribe-app/backend/venv/bin/python` from your worktree's `backend/`.

## 1. Owner decisions (final, 2026-10-27)

- Build voice-match provenance fully for 0.6.0: every speaker identity records where it came from,
  with all metadata, no shortcuts.
- Q1: v449 backfills the legacy `profile_match` auto-accept shape as `voiceprint` and records the
  count (`system_settings` key `privacy.identity_source_backfill`, JSON `revision`, `stamped`,
  `recorded_at`; also logged at upgrade as `v449: recognised N existing speaker identities ...`).
  The upgrade note quotes how to read it.
- Q2: promoted-cluster members count as HUMAN.
- Q3: LLM summaries/topics already generated are not rewritten; disclose it.
- Deletion follows privacy best practice and is AUDITABLE and MEASURED: every purge writes an
  audit event recording the actor, the scope (personal / organization / instance, with its ids),
  the targets, the exact counts removed per store (each Postgres table, each OpenSearch document
  kind), and a post-purge residual verification whose counts must all be 0. The residual is in the
  API response and in the audit event; a non-zero residual makes the purge report FAILURE, never
  success.
- A cross-scope `speaker_match` row that joins a speaker in the purged scope to the same person's
  speaker in another scope IS deleted (voice-derived data of the purged scope), counted separately
  as `cross_scope_matches`. Another tenant's ROWS (speakers, profiles, clusters, docs) are never
  touched.

## 2. The Phase 0 contract you build on

- `speaker.identity_source`: `voiceprint` (machine wrote name + link), `voiceprint_link` (machine
  added only the link to an existing name), `human`, NULL (unknown = human). CHECK
  `ck_speaker_identity_source`.
- `app/utils/speaker_identity_source.py` (pure): `record_machine_identity(speaker, *,
  previous_display_name)`, `record_machine_link(speaker)`, `record_human_identity(speaker)`,
  `after_unlink(speaker)`, `hides_name`, `hides_link`, `visible_profile_id`, `VOICEPRINT`,
  `VOICEPRINT_LINK`, `HUMAN`, `IDENTITY_SOURCES`, `MACHINE_IDENTITY_SOURCES`,
  `BACKFILL_RECORD_SETTING_KEY`.
- `app/services/privacy/redact_view.py`: `PrivacyView`, `redact_speaker_rows` (hides a
  `voiceprint` name+verified+link and a `voiceprint_link` link while off, recomputes status),
  `machine_identity_clauses(view, file_ids_select) -> (name_hidden, link_hidden)`,
  `visible_display_name_expr`, `visible_profile_id_expr`.
- Labels: `canonical_speaker_label_for_row(row, db=None, *, voiceprint_names=None)` is
  policy-aware. Rename trackers compute before/after labels with it, so **set `identity_source`
  BEFORE the "after" label is computed** at every write site.

## 3. Files you own

`app/services/speaker_matching_service.py`, `app/api/endpoints/speaker_update.py`,
`app/api/endpoints/speakers.py` (writes + the name directory query), `app/services/speaker_cleanup.py`,
`app/services/speaker_clustering_service.py`, `app/tasks/speaker_merge_task.py`,
`app/services/privacy/purge.py`, `app/tasks/privacy_purge_task.py`,
`app/services/gdpr_erasure_service.py` (only to add a purge-specific, id-scoped deletion; erasure
semantics unchanged), `app/services/opensearch_service/client.py` (`_speaker_org_filter_clauses`),
`app/schemas/privacy.py` (`PurgeStatus`), `app/api/endpoints/privacy.py` /
`app/api/endpoints/org_admin_policy.py` (purge responses only), the purge UI
(`frontend/src/components/privacy/PurgeConfirmDialog.svelte`, `$lib/settings/privacyPolicy`
helpers, `frontend/src/lib/i18n/locales/*.json` purge keys), `docs-site/docs/features/privacy-controls.md`,
`docs-site/docs/operations/upgrading-to-0-6-0.md`, `CHANGELOG.md`, `backend/app/services/CLAUDE.md`.
NOT yours: `speaker_profile_rename.py` (another lane), `policy_admin.py` (Lane A).

## 4. Work items

**B.1 Every identity write records its source** (all through the helper module; set it before
the tracker's "after" label):

| Site | Call |
|---|---|
| `speaker_matching_service._handle_speaker_match` (auto_accept branch) | `record_machine_identity(speaker, previous_display_name=<value before the write>)` |
| `speaker_matching_service._propagate_profile_assignment` (each updated speaker) | `record_machine_link(speaker)` |
| `speaker_update._apply_high_confidence_match` | `record_machine_identity(..., previous_display_name=...)` |
| `speaker_cleanup.join_cluster_identity` | new required kw-only `source: Literal["voiceprint", "human"]`: `voiceprint` -> `record_machine_identity`, `human` -> `record_human_identity` |
| `speaker_clustering_service._join` (upload `find_or_create_cluster`, Re-cluster Phase 2.5) | `source="voiceprint"` |
| `speaker_clustering_service.merge_clusters` | `source="human"` |
| `speaker_clustering_service.promote_cluster_to_profile` (members) | `record_human_identity` (Q2) |
| `speaker_clustering_service.batch_verify_speakers` accept / name / assign | `record_human_identity`; `skip` unchanged |
| `speakers.py`: `create_speaker` (name given), `update_speaker` (name set, or a `profile_action`), `_handle_create_new_profile_action`, `_accept_speaker_profile_match`, `_create_new_speaker_profile` | `record_human_identity` |
| `speaker_matching_service.assign_speaker_to_profile`, `speaker_update.auto_create_or_assign_profile` | `record_human_identity` |
| `speaker_cleanup.unlink_from_profile` (profile delete, reject, unassign) | `after_unlink(speaker)` after the link and any profile-derived name are removed |
| `speaker_cleanup.detach_from_cluster` / `unassign_speakers` / `split_cluster` | `after_unlink` for every speaker whose link it removed |
| `speaker_profile_rename` | unchanged on purpose (renaming a profile does not confirm a machine link); allowlisted in B2 |
| LLM speaker ID, `batch_speaker_matching.py`, `_handle_no_speaker_match` | suggestion fields only: unchanged |
| diarization row creation, reprocess/rediarize | new rows, NULL: correct |

Semantics a person's action must follow (write a test for each):
- Typing a non-empty name, accepting a suggestion, assigning or creating a profile: `human`.
- Clearing the name (`display_name=""`) without unlinking: a `voiceprint` row becomes
  `voiceprint_link` (the machine link remains and stays hideable/purgeable); a `human` row stays
  `human`; with no link left, `after_unlink` (NULL).
- Unassign / unlink / reject / profile delete: `after_unlink`.
- Speaker MERGE (`speakers.merge_speakers` + `speaker_merge_task`): provenance follows the name.
  Read the merge code and pin what it does: if the target keeps its own name/link, its source is
  unchanged; if the target adopts the source's name or link, it adopts the source's
  `identity_source` too. A person merging two diarized speakers does not confirm a machine name.
- Cluster merge and promote: `human` (a person chose the identity; Q2).
- Known accepted edge (unchanged behaviour): a re-match overwriting a different typed name becomes
  `voiceprint`.

**B.2 Occurrences.** `speaker_matching_service.find_speaker_occurrences`: when the profile's scope
is off (judged like `_scope_gate`: the scope of the profile / the speaker's FILE, never the
caller's argument), exclude `Speaker.identity_source IN MACHINE_IDENTITY_SOURCES`.

**B.3 Name directory.** `speakers.py` grouped query (`label_col`, `has_human_name`, the
`profile_id` filters, `func.min(Speaker.profile_id)`): use `visible_display_name_expr` /
`visible_profile_id_expr` from `machine_identity_clauses(view, <the accessible file subquery>)`.
The admin `/speakers/debug/cross-media-*` routes are operator matching replays: leave them, list
them in the B2 allowlist with that reason.

**B.4 Purge: hide AND delete machine identities, measured and verified.** In
`purge.purge_voiceprints_postgres`, scope speakers ONLY through `PurgeScope.speaker_filters()`
(the file's tenant). New steps, before the suggestion step:
1. `reverted`: `UPDATE speaker SET display_name=NULL, profile_id=NULL, verified=FALSE,
   identity_source=NULL` and, when `suggestion_source IN EMBEDDING_SUGGESTION_SOURCES`, also
   `suggested_name=NULL, suggestion_source=NULL, confidence=NULL` (an LLM suggestion is not voice
   data and stays) `WHERE <scope> AND identity_source='voiceprint' RETURNING id`.
2. `unlinked`: `UPDATE speaker SET profile_id=NULL, identity_source=NULL WHERE <scope> AND
   identity_source='voiceprint_link' RETURNING id` (the name and `verified` are the person's).
3. For `reverted ∪ unlinked`: delete their `speaker_cluster_member` rows in PROMOTED clusters, set
   `speaker.cluster_id=NULL` where it points at a promoted cluster, recount `member_count` of the
   touched promoted clusters, delete promoted clusters left with 0 members (with their centroid
   docs). Human / NULL members stay.
4. `speaker_match`: delete every row with EITHER `speaker1_id` or `speaker2_id` in scope (pin both
   sides in a test); count `matches_deleted` and, separately, `cross_scope_matches` (the other side
   is outside the scope). The other side's speaker row is never modified.
5. Append both id lists to `cleared` so `refresh_cleared_speakers` recomputes status (policy-free:
   `compute_speaker_status(..., voiceprint_names=True)`) and queues the reindex.
6. Never touch `human` / NULL identities. Update the module docstring's "What is deleted" / "Kept".

**B.5 Purge OpenSearch by the Postgres scope (audit item B-a).** Today
`purge_voiceprint_docs` (purge.py ~:396-414) deletes by the documents' `organization_id` stamp
(`gdpr_erasure_service._erase_speaker_voiceprints` ~:117-175), and unpromoted clusters are chosen
by `SpeakerCluster.organization_id` (purge.py ~:240-263). A document or cluster that predates the
tenant backfill is unstamped: an organization purge misses it and a member's PERSONAL purge can
delete it. Required:
- Select speaker docs by the uuids of the scope's speakers (same `speaker_filters()` set), profile
  docs by the scope's profile ids, cluster centroid docs by the ids of the clusters the relational
  half deleted. Add a purge-specific id-scoped deletion; GDPR erasure keeps its semantics
  (`tests/integration/test_deletion_residue_live.py` must stay green).
- Unpromoted clusters: chosen by their MEMBERS (speakers in scope), not by the cluster stamp. A
  cluster whose members are all in scope is deleted with its centroid; a cross-tenant cluster
  (legacy, unstamped, members in several scopes) loses only the in-scope members, and its centroid
  is rebuilt from the remaining members or erased (use `speaker_cleanup`'s reconcile machinery),
  counted as `cross_scope_clusters_trimmed`.
- kNN reads: `_speaker_org_filter_clauses(None)` (`opensearch_service/client.py` ~:82) must not let
  an unstamped document of an ORGANIZATION file match in personal scope. Choose and justify a
  mechanism (for example require an explicit personal stamp, or bound personal kNN to the caller's
  personal file/speaker ids), and test it with an unstamped org-file doc.
- The deletion counts in the audit event are what OpenSearch reports as deleted
  (`delete_by_query` `deleted`), never the number of ids asked for.
- Tests seed documents that are NOT consistently stamped (unstamped org doc, unstamped personal
  doc, a cross-tenant cluster) and assert exactly which survive each scope's purge.

**B.6 Residual verification.** After each pass, in a fresh session and after an OpenSearch index
refresh, measure what the pass was supposed to remove, in scope:
`speaker_identities_machine` (identity_source in the machine set), `voiceprint_suggestions`
(embedding suggestion, not verified, or verified by a voiceprint), `speaker_matches` (either side
in scope), `unpromoted_cluster_members` of in-scope speakers, `profiles_with_embeddings`,
`speaker_docs`, `profile_docs`, `cluster_docs` (for the scope's ids). Every value must be 0.
Non-zero at the FINAL pass: audit outcome `FAILURE` with `reason: "residual"`, task row `failed`,
`PurgeStatus.status = "failed"`, residual shown. Pass 1's residual is recorded too (pass 2 exists
for races).

**B.7 The audit event (both targets).** `purge.audit_purge` details become:
`{"task_id", "phase", "targets", "scope": {"kind", "organization_id", "user_id"},
"counts": {"postgres": {"speaker_identities_reverted", "speaker_links_removed",
"suggestions_cleared", "speaker_matches_deleted", "cross_scope_matches", "speakers_unclustered",
"cluster_members_deleted", "clusters_deleted", "cross_scope_clusters_trimmed",
"promoted_cluster_members_removed", "promoted_clusters_deleted", "profiles_reset",
"statuses_refreshed", "gender_speakers_cleared"}, "opensearch": {"speaker_docs_deleted",
"profile_docs_deleted", "cluster_docs_deleted"}}, "residual": {...}, "residual_ok": bool}`
plus the actor (`user_id`, `username`, `source_ip`, `user_agent` on the request event), the org
stamp and `target_user_id` as today. Counts only, never a name. Assert on the PERSISTED audit
record in tests (the audit sink the existing purge tests use), not on a mock call.

**B.8 Response and UI.** `PurgeStatus` gains `counts` grouped per store (`postgres`,
`opensearch`), `residual`, `residual_ok`, and `audit` (`{"event_type", "task_id"}`). The purge
dialog (`PurgeConfirmDialog.svelte`) shows, once finished, the per-store counts, the residual
check result (a failure state when non-zero) and the audit reference; light/dark; all 12 locales
translated; `npm run check`, `npm run build`, vitest for the formatting helper; open it in a
browser (`DISPLAY=:11`) in both themes before reporting. Regenerate any OpenAPI-derived types the
frontend uses.

**B.9 Writer org-stamp tests (audit item B-b).** Pin that each voiceprint writer stamps the file's
`organization_id`: `tasks/transcription/embeddings.py` (~:362 bulk, ~:278 profile),
`speaker_matching_service` (~:948), `profile_embedding_service` (~:143), `embedding_migration_v4`
(~:1082). Assert on the document body the writer produces (OpenSearch seam captured), one test per
writer, each with an org file and a personal-file control.

**B.10 Docs.** `docs-site/docs/features/privacy-controls.md`: "What is hidden" (names and links a
voiceprint made, everywhere; typed/confirmed names never), "Purging stored data" (machine
identities revert to unnamed/unlinked; the measured counts, the residual check, the audit fields;
cross-scope matches; another tenant never touched), "Audit events" table (add Lane A's
`privacy.labels.resync` row). `docs-site/docs/operations/upgrading-to-0-6-0.md`: which pre-upgrade
identifications are recognised (the D3 shape), that everything else is treated as a person's,
how to read the recorded count (`SELECT value FROM system_settings WHERE key =
'privacy.identity_source_backfill'`, and the upgrade log line), the comparison query below, and
that LLM summaries already written are not rewritten (Q3). `CHANGELOG.md` `[Unreleased]` Fixed +
Changed. `backend/app/services/CLAUDE.md`: the `identity_source` rule, the helper module, the purge
revert.

```sql
SELECT count(*) FILTER (WHERE identity_source = 'voiceprint') AS recognised,
       count(*) FILTER (WHERE identity_source IS NULL AND verified IS TRUE
                        AND suggestion_source IN ('voice_match','profile_match')) AS unknown
FROM speaker;
```

## 5. Tests (each seen RED before the change, then green)

Red-check method: `RED="$(mktemp -d -t ot-redcheck.XXXXXX)" && git archive <phase0-sha> | tar -x -C
"$RED"`, copy ONLY your new test files in, run there: they must fail by ASSERTION. A control the
base already satisfies is shown red by a named one-line mutation in an `ot-redcheck` copy of YOUR
tree (record mutation + failing assertion). Never revert files in place; remove copies after.
OpenSearch/kNN seams are monkeypatched (fixture `speaker_os_seams` in
`tests/fixtures/speaker_cleanup_seams.py`); assertions are on Postgres rows, captured document
bodies, the persisted audit record and the HTTP response.

- B1 `tests/unit/test_speaker_identity_provenance_writes.py`: auto-accept recorded as `voiceprint`
  (real `_handle_speaker_match`), and hidden by `redact_speaker_rows` once off; auto-accept onto a
  typed name records only the link; propagation records a link and never downgrades `voiceprint`;
  retroactive auto-apply is `voiceprint`; upload cluster join `voiceprint`, cluster merge `human`;
  the unlink rule; `test_each_human_action_turns_a_voiceprint_identity_into_a_human_one`
  parametrised over PUT display_name, PUT `profile_action`, verify accept, assign-profile,
  `_create_new_speaker_profile`, batch-verify accept/name/assign, promote (each also: the name
  survives `redact_speaker_rows` while off); clear-name; merge-speakers provenance; split/unassign.
  Red per row: delete that site's helper call in an `ot-redcheck` copy.
- B2 `tests/unit/test_speaker_identity_source_discipline.py`: AST over `backend/app`; every function
  assigning `.display_name` / `.profile_id` on a speaker (attribute, `setattr`, or
  `.values(display_name=|profile_id=)` on `Speaker`) calls a helper from
  `utils/speaker_identity_source` or sets `identity_source`, or is in `_ALLOWED` with a reason.
  Must-fire and must-stay-clean inline fixtures.
- B3 `tests/api/test_privacy_purge.py` (+): purge removes an auto-accepted name and link (seeded
  through `_handle_speaker_match`; counts; reindex callback got the file; `resolved_display_name`
  is the diarizer label); keeps a typed name and removes only a machine link; never touches human /
  NULL (mutation red: drop the `identity_source =` condition); one org's purge leaves another org's
  machine identities; machine members leave a promoted cluster (row gone, `member_count` recounted,
  emptied cluster deleted, human member kept); `cross_scope_matches` counted and the other scope's
  speaker untouched; residual all 0 on success and a forced residual (a row inserted between pass
  and verification, or a seam that skips one delete) makes the response and the audit event report
  failure; the audit record carries actor, scope ids, targets, per-store counts, residual.
- B3b `tests/unit/test_privacy_purge_opensearch_scope.py` (B.5): inconsistently stamped seed docs;
  org purge removes the unstamped org doc; personal purge of a member does not; cross-tenant cluster
  trimmed not deleted; counts equal the seam's reported deletions; personal kNN filter excludes an
  unstamped org-file doc.
- B4 `tests/api/test_speaker_profile_occurrences_privacy.py`: occurrences omit machine links while
  off, include them while on.
- B5 `tests/api/test_speaker_name_directory_privacy.py`: off: "Alice" (machine) absent, the row
  appears under its diarizer label only with `include_unnamed`; on control; a typed "Bob" offered
  while off.
- B6 writer org-stamp tests (B.9) and `speaker_match` both-sides deletion.
- Frontend: vitest for the dialog's count/residual formatting; `npm run check:i18n`.
- Regression: `tests/api/test_voice_match_label_policy.py`, `tests/api/test_privacy_purge.py`,
  `tests/unit/test_privacy_*.py`, `tests/unit/test_speaker_*.py`, `tests/test_speaker_tenant_gating.py`,
  `tests/unit/test_*migration*consistency*.py`, and `tests/integration/test_deletion_residue_live.py`
  on a stack with OpenSearch.

## 6. Test database (must hold v449)

The shared dev stack (`localhost:5176`) does NOT have v449. Use an isolated `--fresh` stack (if
your coordinator allows Docker; export every `*_PORT`) or the no-Docker Postgres Phase 0 used:

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
OpenSearch-backed tests skip there; run them on a stack with OpenSearch before claiming them, and
say which ones you did not run.

## 7. Verification before reporting

1. All B-tests red-then-green with evidence recorded.
2. `cd backend && python3 ../scripts/audit-tests.py tests` = 0 open findings.
3. `scripts/safe-precommit.sh run --all-files` and `... --hook-stage pre-push`: both exit 0.
4. `cd frontend && npm run check && npm run build && npm run test && npm run check:i18n`; the
   dialog opened in a browser in light and dark.
5. Report: commit shas, junit counts, red evidence per test, what was not run and why.

## 8. Final-integration checklist (for the integrator, after Lanes A and B report)

- Merge B then A with `--no-ff`; re-parent v449 onto v448 if wave-1 is present (plan addendum:
  three one-line edits); `python3 scripts/release-tests/lib/alembic-head.py backend` prints one
  head.
- Regenerate whatever the hooks report as drifted (privacy/feature matrices, generated docs tables,
  OpenAPI-derived frontend types for `PurgeStatus`), CHANGELOG `[Unreleased]` complete.
- 12-locale parity AND translation for every new string; `ar` rendered RTL.
- The purge dialog shows the measured per-store counts, the residual result and the audit entry;
  the audit log UI lists `privacy.voiceprints.purge` with those details and
  `privacy.labels.resync`.
- Full gate on a stack holding v449 (`./scripts/run-dev-tests.sh --backend-only` or the
  integration gate, plus `test_deletion_residue_live`), then plan §9's live walk with a browser
  check in both themes.
- `alembic downgrade v447_add_content_hold` (or v448) then `upgrade head` on a fresh stack: the
  backfill record is re-created with the new count.

## Lane A owns (do not edit)

`app/tasks/search_indexing_task.py`, `app/tasks/reindex_task.py`, `app/tasks/rename_propagation_task.py`,
`app/utils/transcript_builders.py`, `app/tasks/summarization.py`, `app/tasks/speaker_identification_task.py`,
`app/services/ingest_artifacts/*`, `app/services/transcript_export_service.py`,
`app/services/transcript_export_turns.py`, `app/services/captions/source.py`,
`app/services/chat/speaker_resolver.py`, `app/api/endpoints/files/*`, `app/api/endpoints/transcript_segments.py`,
`app/api/endpoints/media_collections.py`, `app/services/formatting_service.py`,
`app/api/endpoints/speaker_profiles.py`, `app/services/privacy/label_reindex.py`,
`app/services/privacy/policy_admin.py`, `app/services/privacy/policy.py`,
`app/services/privacy/redact_view.py`, `app/tasks/search_maintenance_task.py`, `app/core/celery.py`,
`app/auth/audit.py`, `backend/app/services/search/CLAUDE.md`, `backend/app/api/CLAUDE.md`,
`docs/design/v060_downstream_changes.md`.
