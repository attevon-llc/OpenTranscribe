# search — the speaker label in the index follows the voiceprint switch (#1280)

Moved intact from `backend/app/services/search/CLAUDE.md`.

## The speaker label in the index follows the voiceprint switch (#1280)

A chunk's `speaker`, its `profile_id`, the digest and facts rosters and the summary leaves are
all DERIVED from Postgres, and all of them can carry a name a voiceprint made
(`speaker.identity_source = 'voiceprint'`) or the profile link it added (`voiceprint_link`).
While a file's voiceprints are off none of that may be (re)written; a switch change rebuilds it.

- **One decision per FILE, resolved by the caller, fail closed.** The writers
  (`tasks/search_indexing_task`, `tasks/reindex_task._extract_file_metadata`,
  `ingest_artifacts/service.load_ordered_segments`, `tasks/summarization._load_summarization_inputs`)
  call `privacy.gates.voiceprints_allowed(file_id, db)` ONCE and pass `voiceprint_names=` to every
  label call (`resolve_chunk_speaker_name`, `_resolve_reindex_speaker_name`, `get_speaker_name`,
  `compute_speaker_stats`, `speaker_namer`). An unreadable policy is "off". The `profile_id` written
  to a chunk goes through `visible_profile_id`. `tests/unit/test_speaker_label_call_site_discipline.py`
  fails on a label call that carries neither the decision nor a session/`PrivacyView`, and on a query that
  reads `Speaker.display_name` without `Speaker.identity_source`.
- **`build_full_transcript` / `build_speaker_segments` need no decision**: they print the raw diarizer
  label (`Speaker.name`, which nothing rewrites). Do not add a display name to them without adding the
  decision.
- **The summary leaves mask the hidden name in the INDEXED copy only.** `_index_summary_plane` asks
  `ingest_artifacts.service.hidden_speaker_names` (hidden `display_name`s and withheld voice suggestions,
  mapped to the speaker's diarizer label; a name another speaker visibly carries is left alone) and
  `index_mapping.mask_names` replaces them whole-word, case-insensitively, in `content` and in the text
  that is embedded. The stored `media_file.summary_data` is NEVER rewritten (owner decision: a summary
  is model output; the docs disclose that a summary generated earlier may still name a person, and
  regenerating it while off produces one without the name). A first name alone is not a hidden name and
  is not caught.
- **A rename while the file is off rebuilds the file.** `propagate_speaker_rename` cannot rewrite in place
  (its `old_names` filter is computed without the policy and would match nothing while reporting success),
  so it queues `queue_transcript_reindex` and returns `{"status": "reindex_queued"}`.
- **A switch change is a durable, audited resync** (`services/privacy/label_reindex.py`, task
  `privacy.label_resync`, CPU queue). `request_label_resync` writes the scope to the pending record
  (`SystemSettings` `privacy.label_resync.pending`, deduplicated by scope) BEFORE dispatching; the task
  pages the affected files (500 per page, by id) through the debounced re-index plus
  `dispatch_file_facts`, counts `ALREADY_PENDING` as done, retires a scope only when every file was queued
  and no newer request arrived, and writes a `privacy.labels.resync` audit event on request and on
  completion with `files_selected` / `files_queued` / `files_already_pending` / `files_failed` / `pages`.
  A scope with a failed file stays pending; `search_index_maintenance` finishes it. The task has no lock on
  purpose (a request that arrives mid-pass must not be skipped); duplicates are absorbed by the debounce.
  Register new tasks in `core/celery.py` `include=` AND `task_routes`: a missing include is silent.
- **Drift scan.** The same maintenance arm runs `detect_label_drift`: for completed files a voiceprint can
  name it compares the labels the policy implies now with the distinct `speaker` values of the file's chunk
  documents (one terms aggregation per 500 files) and queues files that differ. It catches a deployment's
  ceiling resolver or consent hook changing its answer, which no switch hook can see. It resumes from a
  persisted cursor (`privacy.label_resync.drift_cursor`, 5,000 files per tick) and skips a file whose index
  holds no chunks (the unindexed-file arm owns that).
