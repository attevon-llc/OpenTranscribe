# search — switching the embedding model, reindexing, and index-maintenance self-heal

Moved intact from `backend/app/services/search/CLAUDE.md`. Read before touching `model_switch.py`, `tasks/reindex_*`, `search_maintenance_task.py` or anything that creates, deletes or repopulates the chunk index.

## Switching the embedding model (#437) — one implementation, and it fans out

**Clearing a cache re-embeds nothing.** Two vectors from two different models occupy the same
kNN space and `cosinesimil` returns a number for them, so hybrid search ranks the two
populations against each other and nothing looks wrong. There is no error, no log and no metric
in the ordinary run — which is why the provenance below exists.

- **`model_switch.apply_embedding_model_switch` is the ONE switch.** `POST
  /search/models` (the settings UI) and `PUT /search/models/neural/active` (ops-only) both call
  it. They used to be two halves of the job and neither half was a switch: `POST` wrote the
  `search.embedding_model` / `search.embedding_dimension` settings and reindexed but never
  touched the ML active model or the ingest pipeline, so **no vector changed and the setting
  lied**; `PUT` repointed the pipeline and recreated the index but never wrote the settings, so
  the reindex coordinator then read the OLD dimension and recreated the index a **second** time
  at the wrong size — a dimension-changing switch was broken outright.
- **There are two SystemSettings keys and nothing reconciled them.**
  `search.embedding_model` is the HuggingFace name and drives the index's `knn_vector`
  dimension; `search.opensearch_model_id` is the ML Commons id and drives the pipeline. Never
  read the first as "what embedded this document" — it is demonstrably able to disagree.
- **Order is load-bearing.** Settings → pipeline → caches → index → reindex. Every coordinator
  reconciles the index against `get_search_embedding_dimension()`, and
  `recreate_index_for_dimension` *deletes the index* when it disagrees, so writing the setting
  first is what makes the fan-out safe: each coordinator sees a match and no-ops.
- **The reindex covers every owner, not the caller.** `reindex_transcripts_task` filters
  `MediaFile.user_id == user_id`, so the "full reindex" the endpoints advertised covered only
  the admin who pressed the button — on a multi-user deployment that left every other user's
  chunks in the previous model's vector space, permanently, via the documented happy path.
  `dispatch_reindex_for_every_owner` dispatches one coordinator per owner of a COMPLETED file.
  N concurrent coordinators is not a new shape: `tasks/search_maintenance_task._dispatch_reindex_tasks`
  already loops one per user.
- **An undeployed model is refused (409), not recorded.** Saving a selection for a model that
  cannot embed anything is what made the legacy path *destructive*: the coordinator honours the
  new dimension, deletes the whole chunks index, and then fails every write because the
  untouched pipeline still emits the old width. The settings UI can now satisfy the guard
  itself (#453): the picker carries per-model `ready`, badges unready models, and offers
  **Download & deploy** (idempotent register+deploy). Before that, the guard's 409 told the
  admin to POST two endpoints by hand — and those endpoints 404ed for every model anyway
  (`{model_name}` never matched the `/` every registry key contains), on top of
  `find_model_by_name` returning ML Commons *chunk* ids that made deploy 500. All three are
  fixed and pinned by `tests/unit/test_embedding_model_admin_reachable.py`.

### `embedding_model` is the model, and `"neural"` means UNKNOWN

The chunk mapping has always declared `embedding_model` as a `keyword` and the write site
always filled it with the string `"neural"` — the embedding *mode*. Measured before the fix:
**210,908 documents, cardinality 1**.

- `active_embedding_model()` returns what `ensure_neural_ingest_pipeline` resolved **from the
  pipeline it wrote**, cached beside `_neural_pipeline_available` and cleared by
  `reset_neural_pipeline_state`. A stale label is worse than none — it is a specific claim that
  gets believed.
- The label is the model **name**, not the ML id: re-registering one model yields two ids and
  identical vectors, so an id-keyed label would report a mix where there is none.
- **`"neural"` is kept as the unknown bucket.** Not replaced with a new sentinel (that splits
  the unattributed population in two) and never backfilled with the current model (that asserts
  something about 210,908 documents nobody can know). Unresolvable new writes land there too.
- **No mapping change, so no `_INDEX_VERSION` bump.** The index `_meta` deliberately does *not*
  record the model either: that is a second copy of a fact that can drift from the documents,
  and the `terms` agg reads the documents themselves. Derive, don't record.

### Detecting a mixed index

`survey_embedding_models()` — one bounded `terms` agg (`size: 50`, `missing` bucket for
documents with no field at all), **2.5–6.7 ms** measured against the 210,908-document index.

| verdict | meaning |
|---|---|
| `empty` / `unavailable` | no documents / could not ask — `unavailable` never reads as agreement |
| `unattributed` | only `"neural"`. Every deployment's state before its first post-fix index |
| `uniform` | exactly one named model, no unknowns. The only `comparable` verdict besides `empty` |
| `partially_unattributed` | one named model **beside** the unknown bucket |
| `mixed` | **two or more NAMED models.** Proven, not suspected |

**`partially_unattributed` is deliberately not `mixed`.** Those unknown documents might be from
the same model, and it is the state every existing deployment enters the moment it indexes
anything after this lands — an alarm that fires wrongly for the universal case is one people
learn to ignore in the dangerous one.

Read at `GET /search/models/neural/status` (the endpoint an operator consults before deciding
to reindex), in both switch responses, and once per `search_index_maintenance` tick — the only
**automatic** detector, because `_get_indexed_uuids` only finds files with *no* chunks, so a
file holding old-model chunks looks indexed forever. It logs and does **not** act: dispatching
a full re-embed of every corpus from a beat tick on one aggregation is how a health check
becomes an outage. It is deliberately **not** in `POST /search/repair-indices`, whose machinery
is close/reopen/force-merge and cannot repair a vector space — reporting it there would imply
the repair had addressed it.

`ml_ingest_pipelines.get_active_model_id()` **refuses to guess.** It used to return
`list_models(deployed_only=True)[0]` — the first hit of an unsorted `match_all` — whenever the
stored model was not deployed, and `ensure_neural_ingest_pipeline` writes whatever it is handed
straight into the pipeline. That silently repointed embedding **with no user action**. Exactly
one deployed model is not a choice and is adopted with a warning (the recovery the fallback
exists for); more than one returns `None` and leaves search on BM25 — loud, obvious and
reversible, where a wrong guess is silent and costs a full re-embed.

## A reindex is corpus-wide, in every mode (#627)

**`POST /search/reindex` had the identical defect #437 fixed for the switch, and kept it for
another release.** All three of its modes dispatched one
`reindex_transcripts_task.delay(user_id=current_user.id, ...)`, and that coordinator filters
`MediaFile.user_id == user_id`. So the admin Settings → Search "Reindex all" button repaired the
pressing admin's own account and left every other user's files exactly as they were — no error,
no warning, a success toast. The pending-only sweep was scoped twice over (a `user_id ==` in
Postgres *and* a `{"term": {"user_id": ...}}` beside the chunk-plane clause in the aggregation),
so it could not even see another account's unindexed files. Naming another user's file UUID
explicitly did nothing at all.

- **One fan-out loop, not two.** `dispatch_reindex_for_every_owner` grew an optional
  `file_uuids_by_owner`; it did **not** grow a sibling. `None` still means the whole corpus
  (every owner of a COMPLETED file, caller first and unconditionally). A mapping means a partial
  scope, one coordinator per named owner with that owner's files.
- ⚠️ **An owner with an empty list is DROPPED, never dispatched.**
  `reindex_transcripts_task` narrows with `if file_uuids:`, so `[]` is indistinguishable from
  "no filter" and would re-embed that owner's entire account — the opposite of what a
  pending-only sweep asked for.
- **`reindex_scope.py` answers the scope question**, the dispatcher answers the fan-out one.
  `pending_files_by_owner()` distinguishes "nothing indexable exists" from "everything is
  already indexed"; the endpoint reports them as different messages, because a corpus-wide sweep
  answering the empty-deployment message would be hiding a survey that saw nothing.
  `indexed_file_uuids()` **fails open** — an unreachable cluster reads as "nothing is indexed"
  and queues the corpus rather than nothing. That is the pre-existing direction and the safe one
  (re-indexing overwrites by deterministic id), but the blast radius is now deployment-wide, so
  the failure is logged with a traceback.
- **The gate is `get_current_admin_user`, and it always was.** Corpus-wide work behind a
  per-user gate would trade a scoping bug for a privilege one;
  `test_reindex_is_refused_for_a_plain_user` carries the two-owner corpus fixture so a dropped
  gate shows up as dispatches for *other* owners, not merely a wrong status code.
- **`POST /search/reindex/stop` cancels the whole run it started** (#691). It was left per-owner
  by #627 — the caller could only flag their own coordinator while the other owners' ran to
  completion — because cancelling a fan-out needs the dispatched owner set and nothing persisted
  it. `dispatch_reindex_for_every_owner` now writes its `{owner: task id}` mapping to
  `reindex_fanout:{admin id}` as well as returning it, and `stop` flags every owner in it. See
  the next section for the two properties that make that safe.

## Cancelling a fan-out: the flag NAMES the run (#691)

`reindex_cancel:{user_id}` used to hold `"1"`. It now holds **the coordinator task id being
cancelled**, and that is not decoration — without it the fix does not work on the deployment
shape it was written for.

- ⚠️ **The coordinator that most needs to see the flag has not started yet.** The fan-out
  dispatches the caller first, so with fewer workers than owners the caller's coordinator runs
  while owners B..N sit in the broker queue. Every coordinator **clears** the cancel flag on
  entry — a flag left by an *earlier* run would otherwise abort the next legitimate reindex after
  its first file — so a flag carrying only `"1"` would be erased by the very coordinator it was
  written for. Naming the run is what distinguishes "I was cancelled before I started" (abort,
  release the lock, index nothing) from "someone else's flag is lying around" (clear it and
  proceed). `reindex_cancel.consume_pending_cancel` is that single decision, called once at
  coordinator entry in place of the old unconditional clear.
- **A second run started while one is cancelling is unaffected, by construction.** Its
  coordinators carry fresh task ids, so the first run's flags read as stale and are cleared
  exactly as they always were. No run can inherit another run's cancellation, and there is no
  window to reason about.
- **The record is per triggering admin**, so two admins' concurrent fan-outs cancel separately.
  Overlapping *owners* are not a hazard either: `reindex_lock:{user_id}` already makes two
  coordinators for one owner mutually exclusive — the second is skipped, never concurrent.
- **A caller with no recorded fan-out still cancels their own coordinator**, with the legacy `"1"`
  value. That is the `search_index_maintenance`-dispatched run, which has no admin-facing run id;
  its behaviour is unchanged from before #691. Those automatic per-owner runs are deliberately
  **not** swept up by an admin's stop — the button cancels the run *you* started.
- **`reindex_fanout:*` is NOT in `app/startup/maintenance.py`'s startup sweep**, unlike `reindex_cancel:*`. The
  API process restarts independently of the Celery workers still executing the fan-out, and
  deleting the record takes the only handle `stop` has on those coordinators with it. A 1-hour
  TTL bounds it instead.

## The bootstrap self-heals, and why it is a beat task (#625)

**The old bootstrap was a one-shot startup task with no retry.** `app/startup/warmups.py`'s
`_initialize_neural_search` fired exactly once from `lifespan` via `asyncio.create_task`, and
every failure arm was a bare `return` after a `logger.warning`. Nothing ever re-entered it.

The specific race: `ml_model_registration._REGISTRATION_MAX_WAIT` (300s) is a poll **ceiling on our
own polling**, not on OpenSearch's registration task, which keeps running server-side after we
stop looking. A model can finish `REGISTERED, deployed=False` a few minutes after the API
process gave up, and then sit there forever — nothing re-checks it. Consequence: the ingest
pipeline never gets created, and every file indexed during that window is written with
`use_neural=False` / `embedding_model` absent, **permanently** — `search_index_maintenance`
(above) only finds files with **no** chunks, never files with text-only ones.

**The fix is one idempotent function, two callers**, not two implementations:
`services/search/neural_bootstrap.py`'s `ensure_neural_search_bootstrap` owns the whole
sequence (managed-mode adoption, ML settings, local-model scan, download,
`ensure_model_deployed`, `set_active_model_id`, `ensure_neural_ingest_pipeline`) that used to
be inlined in `_initialize_neural_search`. It is a **sequencer**, not a state machine —
`ensure_model_deployed` and `ensure_neural_ingest_pipeline` are already fully
idempotent/resumable, so this module reimplements none of that.

- **Caller 1: the startup fast path.** `app/startup/warmups.py::_initialize_neural_search` sleeps
  `NEURAL_BOOTSTRAP_STARTUP_DELAY_SECONDS` (15s), then calls the same function once via
  `run_in_threadpool`, elected with `run_once_per_boot("neural_search_bootstrap")` so N
  replicas booting together don't all race the same expensive OpenSearch calls. This is no
  longer the ONLY attempt.
- **Caller 2: the beat self-heal**, `app/tasks/search_maintenance_task.neural_search_bootstrap_task`,
  every 10 minutes (`crontab(minute="3,13,23,33,43,53")`). A healthy deployment pays only
  `neural_search_ready()` — the cheap probe (`get_active_model_id()` +
  `is_neural_pipeline_available()`, no OpenSearch mutation) — on every tick, forever. Only a
  miss runs the expensive arm.

⚠️ **Two callers means a race, and the race is DESTRUCTIVE, not just wasteful (issue #625
follow-up).** `find_model_by_name` matches a model the instant ML Commons creates its meta
document — state `REGISTERING`, long before there is anything to deploy. `ensure_model_deployed`
used to see "matched, `deployed` is False" and call `deploy_model` unconditionally. A deploy
issued into a `REGISTERING` model does not merely fail: OpenSearch's own deploy-failure cleanup
runs `ModelHelper.deleteFileCache(modelId)`, which **recursively deletes**
`ml_cache/models_cache/register/<modelId>/` — the exact directory the OTHER caller's in-flight
download is still writing into. Measured on `opensearch:3.4.0`: the control (no concurrent
deploy) registers in ~12-18s every time; forcing a second deploy ~3s into registration turns it
into `REGISTER_MODEL FAILED: <path>.zip (No such file or directory)` plus a `DEPLOY_MODEL`
NPE (`totalChunks is null`) — the exact pair a real fresh-install rehearsal hit live, in ~6
seconds. **No poll duration can rescue this**: the files are gone, so there is nothing left to
wait for. `run_bootstrap_tick`'s 600s backoff after one failure happened to match
`test-fresh-install.sh`'s 600s poll window exactly, which is why one race guaranteed the
rehearsal's assertion would fail.

The fix is a state guard, not a lock alone: `ml_model_deployment._DEPLOYABLE_STATES` /
`_IN_FLIGHT_STATES` plus `_await_deployable_state()` make `ensure_model_deployed` wait out
`REGISTERING`/`DEPLOYING` rather than deploying into it, so the destructive path is closed even
if two callers still overlap. **Defense in depth, not a substitute**: `_initialize_neural_search`
also takes `search_maintenance_task.NEURAL_BOOTSTRAP_LOCK_KEY` — the SAME lock the beat task
already held via `@with_task_lock` — so the two callers exclude each other instead of relying
purely on the state guard to save them. `run_once_per_boot` only ever protected N replicas'
startup paths from racing EACH OTHER; it did nothing about one replica's own startup path racing
its own beat tick, which is the race that actually shipped.

**The backoff never terminates.** `run_bootstrap_tick` tracks `attempts` / `next_at` /
`last_error` in Redis (`neural_bootstrap:*` keys, 24h TTL), doubling from
`NEURAL_BOOTSTRAP_BASE_BACKOFF_SECONDS` (600s) and saturating at
`NEURAL_BOOTSTRAP_MAX_BACKOFF_SECONDS` (6h) — it never gives up the way the old one-shot did.
Redis failure **fails open** (attempts the bootstrap anyway), the same rule
`app/utils/boot_once.py` documents: duplicated work is safer than skipped work.

**No new `SystemSettings` row for bootstrap health** — matches this package's "derive, don't
record" rule (see the mixed-index detector above). `bootstrap_status()` re-derives `state` from
the same cheap probe on every read; only the attempt *history* (genuinely not derivable) lives
in Redis. `GET /search/models/neural/status`'s `bootstrap` block and the admin Settings → Search
banner both read it.

**The explicit non-decision: no auto re-embed of the text-only tail.** `bootstrap_status()`
reports `text_only_chunk_files` — a distinct-file count of chunks with no `embedding_model`
field, i.e. files indexed while the pipeline was down — but nothing dispatches a reindex for
them automatically. Same reasoning as the mixed-index detector: dispatching a full re-embed
from a beat tick is how a health check becomes an outage, and here it's worse, because an
operator hasn't even chosen to look yet. An operator-triggered re-embed action for exactly this
count is tracked separately, issue #626 — do not build it as part of this mechanism.

⚠️ **`text_only_chunk_files` counts `EMBEDDING_MODEL_ABSENT`, never `EMBEDDING_MODEL_UNKNOWN`
("neural").** The `"neural"` sentinel means "embedded, but by a pre-#437 pipeline that never
recorded provenance" — a much larger, unrelated population on any index older than #437.
Counting it here would vastly overstate this specific bootstrap-gap defect.

## A failed index must not report success (#495)

`index_transcript_chunks` used to `except Exception: return 0`, and its caller
`tasks/search_indexing_task` had an arm that wrapped a bare int as `{"chunk_count": result}`,
marked the DB task **completed** and returned `{"status": "success"}`. So a dead OpenSearch, a
mapping rejection or a partial bulk load were all reported to the user, the task table and the
notification as a **successful index of zero chunks**. The task's own `except` could never fire.

What made it invisible is worth keeping in mind generally: `0` was *also* the legitimate answer
for "no client", "no segments" and "no chunks generated". A sentinel that collides with a real
value cannot be checked. So:

- Failures **raise**. `_bulk_index_chunks` returns how many documents landed;
  `indexed < len(chunks)` — the state `_retry_failed_docs` leaves after it gives up at 2
  attempts — raises rather than returning short. Safe to retry: ids are deterministic
  (`{file_uuid}_{chunk_index}`), so a re-run overwrites rather than duplicates.
- The three "nothing to index" outcomes return a dict carrying an explicit `reason`.
- **The digest plane reports the true count but does NOT raise**, and the asymmetry is
  deliberate: `_index_digest_plane`'s `except` already declines to fail an index over derived
  enrichment, whereas the chunks are the transcript itself. It previously discarded
  `_bulk_index_documents`'s return value and reported `len(documents)` — the number of sections
  *generated*, identical whether all or none were written.

⚠️ **Tests must query a PLANE, not a bare `file_uuid` term.** #495 was filed as "the synthetic
corpus indexes no chunks" and marked `xfail(strict=True)`; the chunks were there the whole time.
A bare `{"term": {"file_uuid": ...}}` returned **8 chunks and 1 digest**, and the digest has no
`speaker` field, so the test raised `KeyError: 'speaker'`. Under `-q --tb=line` that showed as a
bare `FAILED` and the passing `assert hits` above it was read as the failure. Use
`chunk_plane_query` — the same rule the product follows — and assert `hits` non-empty on its own
line so "nothing indexed" can never again be confused with "wrong plane".
