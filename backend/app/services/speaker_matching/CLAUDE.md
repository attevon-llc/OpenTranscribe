# services/speaker_matching/ — implementation of `SpeakerMatchingService`

`speaker_matching_service.SpeakerMatchingService` is the one public entry. It keeps `__init__`,
the privacy/tenant gate (`_scope_gate`, `_voiceprints_allowed`, `_media_file_org_id`) and
`get_confidence_level`, and binds every other method from here as a class attribute. Each
module holds plain functions taking the service as `self`; callers and `patch.object` on the
class are unchanged.

| Module | Concern |
|---|---|
| `thresholds.py` | `ConfidenceLevel`, `MATCHING_ORIGIN_TASK` |
| `match_queries.py` | voiceprint -> known speakers / profiles (read-only) |
| `profiles.py` | assigning a speaker to a profile, profile occurrences |
| `processing.py` | per-file entry point (`process_speaker_embeddings_native`) and the per-speaker match-or-store step |
| `match_handling.py` | applying a found / not-found match: user corrections that block it, the origin record it leaves, voiceprint store, profile centroid blend |
| `propagation.py` | spreading a profile assignment to similar speakers |
| `match_storage.py` | speaker-to-speaker match pairs: kNN query, stored rows |

Mock targets: `add_speaker_embedding` is imported into `match_handling`; `find_matching_speaker`
into `match_queries` AND `match_storage` (patch both when a flow crosses them);
`embedding_mode_service.voiceprint_model_id` is called through its module so one patch covers
all three callers. Tenant/privacy gating is verbatim: every public entry still goes through
`_scope_gate` / `_voiceprints_allowed`; OpenSearch scores convert via `app.utils.cosine_space`.

## Removing a voice: `speaker_cleanup.py` is the only implementation (issue #1214)

A speaker's voice is copied into more than its own voiceprint document: every cluster it
belongs to stores a `cluster_<uuid>` centroid (a weighted mean of the members' voiceprints,
profile-linked members counting double), its profile stores a `profile_<uuid>` embedding (a
mean of the members'), and the transcript index planes carry its label. Deleting the speaker
row, or moving it out of a cluster or profile, changes none of those by itself — and the
`ON DELETE CASCADE` on `speaker_cluster_member` erases the only record of which centroids
averaged it in. #1211 fixed this for the file purge alone; every per-speaker path kept the
removed voice inside some aggregate. Split by concern: `speaker_cleanup.py` (capture + reconcile), `speaker_identity_rules.py` (blacklist / cannot-link checks, `record_unassignment`), `speaker_identity_moves.py` (`unlink_from_profile`, `join_cluster_identity`, `detach_from_cluster`).

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
propagation), `speaker_embedding_reassignment_task` (segment-reassign voiceprint drift),
`files/reprocess.py` (transcription and rediarize clears), `file_retry.reset_file_for_retry`,
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
- **And commit right after the writes.** A link flushed but not committed holds the speaker's
  row lock, and a merge's `DELETE FROM speaker` of that row waits on it. The label task's
  `_update_profile_embedding` once flushed, ran the privacy and impact reads, and committed
  inside the rebuild. Its callers now read first and it commits before the rebuild
  (`tests/unit/test_speaker_profile_assignment_lock_scope.py`).
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

The owner's rule, implemented once, in `speaker_identity_moves.detach_from_cluster`:

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
| **An automatic path never overwrites an identity a speaker already has**: `has_other_identity` = linked to another profile, or named (typed, or an accepted suggestion) something other than the profile. `find_or_create_cluster`, Re-cluster Phase 2.5 and profile propagation skip such a speaker; `merge_clusters` leaves it in the source and reports it (except members of the SOURCE's own profile — merging two named clusters says the names are one person) | `speaker_identity_rules.has_other_identity` / `identity_conflicts_with_cluster` |
| **Decision 5: a speaker linked to another profile is no longer in the old profile's cluster.** `reconcile_speaker_impact` releases, from every promoted cluster it refreshes, members linked to a different profile — so renaming, assigning, accepting, batch-assigning or retroactively matching a member away takes it out (no blacklist: the new link already says who it is) | `speaker_cleanup._release_members_of_other_profiles` |
| **Rejecting an identification is an unassignment**: `verify?action=reject` leaves the linked profile's clusters through `detach_from_cluster` (blacklist both halves) and records a profile blacklist (`rejected_by_user`) for the linked profile and the profile its suggestion named | `speakers._reject_speaker_suggestion` |
| **A moved segment leaves the old speaker's voiceprint**: the reassignment task removes the segment's embedding from the source by the exact inverse of the weighted add the target gets, and both rewritten documents keep the file's tenant (they were rewritten without `organization_id`, i.e. as personal) | `speaker_embedding_reassignment_task._remove_segment_from_voiceprint` |
| **A correction survives a re-run of the file** (rediarize, re-transcribe, retry), which deletes the speakers and, by CASCADE, their corrections: stashed by the corrected speaker's time spans before the delete, re-applied when the re-run saves its segments to every new speaker whose speech is ≥ `INHERIT_SHARE` (0.5) inside them | `speaker_correction_carryover` (table `v438`) |
| Suggestions never offer a refused identity (speaker list, profile suggestions endpoint), and the voice-match suggestion of `_handle_no_speaker_match` (a >=0.75 voiceprint one moves the canonical label) skips a refused name or cannot-link partner | `SmartSpeakerSuggestionService.without_refused`, `SpeakerMatchingService._refusals` |

Deliberately unchanged: deleting a cluster records nothing (it discards a grouping; it
is not a statement that the members differ), and `blacklist: false` on unassign records
nothing (the UI's "Prevent from rejoining" opt-out).

**Autoflush is off.** A caller that sets `profile_id` and rebuilds in the same transaction
must flush first (`speakers_profile_workflow._update_profile_embedding` does): the rebuild otherwise
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
  (`speaker_roster.build_candidate_roster`): a "who said X" question must not resolve a speaker
  through a guess.
- **The LLM task will not overwrite a voiceprint suggestion** — that would replace the evidence
  the label rests on with a guess.
- **A voiceprint-derived name obeys the privacy switch (#1280).** `canonical_speaker_label_for_row(row, db=None)`
  leaves a `voice_match`/`profile_match` name out while the row's FILE has voiceprints off (the row stays unverified; a
  verified one is a person's and stays) and asks the policy only when that suggestion is what decides the label. An ORM
  `Speaker` finds its own session; a columns-only row must select `verified` and `media_file_id` and be given `db` (or a
  shared `PrivacyView`). Every printer of a label goes through it (exports, captions, the chunk and reindex writers,
  `get_speaker_name`, the chat roster, `SpeakerStatusService`), so a new one must too. In a response, redact first and
  decorate second: `add_computed_status` derives `status_text` and `resolved_display_name` from the suggestion that
  `redact_speaker_rows` withholds. A voiceprint switch change rebuilds the affected labels through a durable, audited resync
  (`run_label_resync` in `services/privacy/label_reindex.py` + `tasks/privacy_label_resync_task.py`, registered in the Celery `include=` list): the request
  records a pending scope (`privacy.label_resync.pending`), the task re-indexes the selected files through the existing debounced
  dispatch and writes `privacy.labels.resync` audit events (requested, finished: counts of files selected/queued/already
  pending/failed, never a name or a file), and drift detection in the search-maintenance task finishes a rebuild that could not run.
  Responses hide immediately; the index follows in the background.
- **Every writer of `Speaker.display_name` / `profile_id` records who wrote it (`speaker.identity_source`, #1280).** Values:
  `voiceprint` (a voiceprint wrote name AND link), `voiceprint_link` (only the link, onto a name that was there), `human`, NULL
  (unknown, read as human). Set it ONLY through `utils/speaker_identity_source.py` (`record_machine_identity`,
  `record_machine_link`, `record_human_identity`, `after_unlink`, `after_name_cleared`), and BEFORE a rename tracker computes its
  "after" label, because the label depends on it while voiceprints are off. Voiceprint paths (auto-accept, propagation, retroactive
  matching, upload-time cluster joins: `join_cluster_identity(..., source="voiceprint")`) record machine; a person's action
  (typing, accepting, assigning, creating a profile, batch-verify, promote, cluster merge) records human. Merging two speakers
  leaves the survivor's source alone (it keeps its own name and link). `tests/unit/test_speaker_identity_source_discipline.py` is an
  AST gate: a function that assigns either field without calling the helpers or setting `identity_source` must be in its `_ALLOWED`
  table with a reason. Readers hide a voiceprint-made name/link while the file's voiceprints are off (`redact_view`,
  `machine_identity_clauses` for queries that never load rows: the speaker name directory and profile occurrences use them); a purge
  reverts `voiceprint` rows (unnamed, unlinked, unverified, voice suggestion cleared) and unlinks `voiceprint_link` rows together
  with the voice suggestion and confidence of the match that added the link (`hides_voiceprint_suggestion` hides ANY voice-sourced suggestion while off, whoever verified the speaker; the final purge pass
  clears the four suggestion columns of every remaining voice suggestion, a person-verified speaker's included, never its name, `verified`,
  link or identity record; `record_suggestion` refuses to write one onto a person-verified speaker unless `identity_follows=True`, and
  `canonical_speaker_label` takes no `verified` argument any more). An Inbox skip is a review marker, not a confirmation: `after_skip` marks a name-less, link-less row that carries a voice
  suggestion `voiceprint`, so the suggestion stays the machine's. A profile rename (`speaker_profile_rename`) reaches only members
  whose link is visible under their file's decision. Every cross-recording reader (`/speakers/{uuid}/cross-media` and its debug
  twins, the speaker media preview, the merge-completion event, the topic-extraction prompt) goes through `hide_machine_identities`
  / `get_speaker_name` for its OWN file; none matches on the raw `display_name` / `profile_id`. v449 backfilled only rows with
  positive evidence (the shape, the profile older than the speaker, an earlier linked speaker: a typed name that a legacy re-match
  rewrote has the shape but fails the evidence) and recorded `stamped` and `shape_left_unmarked` in `system_settings` key
  `privacy.identity_source_backfill`; a purge cannot be undone, so the backfill errs toward NULL.
- **The record of HOW an identity and a suggestion came to be (v449, same issue).** Beside
  `identity_source` the row keeps `identity_source_at`, `identity_source_user_id` (the person; NULL for
  a task or a deleted account; FK `ON DELETE SET NULL`, deliberately unindexed) and
  `identity_source_detail`, and `suggestion_detail` for the recommendation in
  `suggested_name`/`suggestion_source`. All are written ONLY by the helpers in
  `utils/speaker_identity_source.py`: `record_human_identity(speaker, *, user_id, origin)` (user id and
  origin are required; `keep_recorded=True` is for the later step of the same action),
  `record_machine_identity/record_machine_link(speaker, ..., detail=)`, `after_unlink`/`after_name_cleared`/
  `after_skip`, and for a recommendation `record_suggestion`, `clear_suggestion`, `mark_review_skipped`
  (SQL twins for the purge: `cleared_provenance_values`, `cleared_suggestion_values`). The detail
  is built ONLY by `utils/speaker_identity_detail.py` (ids and identifiers, never a name or transcript
  text; a closed `origin` vocabulary; an accepted suggestion is summarised WITHOUT matched ids so a
  person's record stays valid while voiceprints are off). Pass the request user's id from an endpoint;
  a Celery task passes none and names itself in `origin_task`. Call `record_human_identity` BEFORE
  `clear_suggestion`: it snapshots the suggestion the person accepted. While a file's voiceprints are
  off the record of a machine identity and the detail of a voice suggestion are hidden with the name and
  link (`redact_view.hide_machine_identities`/`redact_speaker_rows`, `hides_provenance`), the purge clears
  and counts them in the same UPDATE, and `tests/unit/test_speaker_provenance_never_indexed.py` fails if
  an index or prompt builder names them. The API `identity`/`suggestion` objects are attached by
  `speaker_provenance_view.attach_speaker_provenance(db, rows, viewer)` AFTER the redaction;
  `identity.by` is shown only to the file's owner, an org admin of its organization and a platform admin whose reach covers the file (`_can_see_actor` asks `bypass.allows`: single mode any admin, multi mode only under a grant, which records the use; a bare `user_is_admin` is NOT the test), and an account named by an erasure request is the `{"erased": true}` marker, never a name.
  The two JSONB columns are `JSONB(none_as_null=True)`: a plain JSONB stores Python `None` as the JSON
  value `null`, which is not SQL NULL and made the purge's residual check see a cleared record. This is
  the CURRENT state; the query review is #1337. Every change is ALSO appended to
  `speaker_identity_event` (#1336, v462): the helpers leave an attribution note on the row and the
  Session `before_flush` listener in `models/speaker_identity_event.py` writes the row in the same
  flush, so the non-restamping helpers (`after_unlink`, `after_name_cleared`, `clear_suggestion`,
  `mark_review_skipped`, `after_skip`) take a REQUIRED `user_id`. A bulk `UPDATE` must also call
  `services/speaker_identity_history.record_bulk_identity_events` (the AST gate checks it).
  The AST gate also now rejects a direct assignment to any of these columns and does not accept a
  reader helper (`hides_link`) as "recording".
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
