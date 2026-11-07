# Issue #1337: review every application query for plans, indexes and N+1 patterns

Status: PLAN ONLY, written 2026-11-07 on `v070/1337-query-plan` (worktree
`/mnt/nvm/repos/transcribe-app-wt-v070-1337-plan`, tag `cleanup-wave-af14`). No application code,
migration or test is changed by this document. Milestone v0.7.0 (due 2026-11-30).

An implementer with no conversation memory should be able to execute this file top to bottom.
Every `file:line` below was read on 2026-11-07 at `865afd59b`; line numbers drift, so re-locate by
the quoted symbol before editing.

## 0. Summary

| Item | Decision |
|---|---|
| Method | Seed a synthetic 100k-file / 1M-speaker dataset on an isolated `--fresh` stack, turn on `pg_stat_statements` + `auto_explain` there only, drive the hot endpoints, rank statements, then `EXPLAIN (ANALYZE, BUFFERS)` the top ones against a saved snapshot. Every fix is measured before and after on the same snapshot with a control query. |
| First example (#1328) | **Already implemented** as `v458_speaker_machine_identity_index` + `backend/app/db/speaker_indexes.py`, built after the upgrade by `migrations._retry_concurrent_indexes` (`backend/app/db/migrations.py:269`). The issue is still open because the at-scale before/after measurement was never recorded. Lane L0 takes that measurement and closes #1328 with numbers. |
| Index mechanism | The repo's own concurrent-index pattern (`app.db.concurrent_indexes.ensure_concurrently`, precedents v458, v463, v467), not `op.create_index` inside the revision. Section 5.2 explains why and cites the Alembic `autocommit_block` alternative that was considered. |
| Regression checks | (a) statement-count assertions that must not grow with row count, built on SQLAlchemy `before_cursor_execute` events (the pattern `tests/unit/test_privacy_purge_opensearch_scope.py:335` already uses); (b) structural plan assertions (`Anti Join` present, `SubPlan` absent) on the shared test DB; (c) index-usability assertions under `SET LOCAL enable_seqscan = off` in a private scratch database, the pattern `tests/unit/test_v458_migration_consistency.py:207` already uses. |
| Lanes | L0 tooling + baseline (wave 0), L1 gallery + tags, L2 speakers, L3 tasks + admin (wave 1, parallel), L4 privacy/purge rewrites, L5 measured indexes (wave 2). One full sweep per wave. |
| v0.7.0 scope | Measurement, N+1 fixes, `NOT IN` and OR-of-subquery rewrites, SQL pagination of `GET /tasks`, indexes that the measurement justifies. Trigram search indexes, keyset pagination, FK `ON DELETE CASCADE` + `passive_deletes`, and shipping `pg_stat_statements` on by default are later (section 6.4). |

## 1. Documentation consulted (house rule: docs first)

PostgreSQL 17 (the image is `postgres:17.5-alpine`, `docker-compose.yml:12`):

- `pg_stat_statements`: https://www.postgresql.org/docs/17/pgstatstatements.html (must be in
  `shared_preload_libraries`, needs `CREATE EXTENSION`, `pg_stat_statements_reset()`).
- `auto_explain`: https://www.postgresql.org/docs/17/auto-explain.html (`log_min_duration`,
  `log_analyze`, `log_buffers`, `log_format`, `log_nested_statements`; the docs warn that
  `log_analyze` adds overhead to every statement, so it is a measurement-stack setting only).
- `EXPLAIN`: https://www.postgresql.org/docs/17/sql-explain.html and
  https://www.postgresql.org/docs/17/using-explain.html (`ANALYZE`, `BUFFERS`, `SETTINGS`,
  `FORMAT JSON`; "EXPLAIN ANALYZE actually executes the statement", so writes are wrapped in
  `BEGIN; ... ROLLBACK;`).
- Slow-statement log: https://www.postgresql.org/docs/17/runtime-config-logging.html
  (`log_min_duration_statement`) and `track_io_timing` in
  https://www.postgresql.org/docs/17/runtime-config-statistics.html.
- `ALTER SYSTEM`: https://www.postgresql.org/docs/17/sql-altersystem.html (writes
  `postgresql.auto.conf`; command-line `-c` flags take precedence over it, which matters because
  `docker-compose.yml:21-32` passes several `-c` flags but no `shared_preload_libraries`).
- Planner switches: https://www.postgresql.org/docs/17/runtime-config-query.html
  (`enable_seqscan = off` does not forbid a sequential scan, it makes the planner avoid one when
  any alternative exists).
- `CREATE INDEX CONCURRENTLY`: https://www.postgresql.org/docs/17/sql-createindex.html#SQL-CREATEINDEX-CONCURRENTLY
  (cannot run in a transaction block, waits for every transaction that could use the index, a
  failed build leaves an INVALID index that must be dropped).
- Partial indexes: https://www.postgresql.org/docs/17/indexes-partial.html (the planner uses a
  partial index only when it can prove the query's WHERE implies the index predicate).
- Statistics views: https://www.postgresql.org/docs/17/monitoring-stats.html
  (`pg_stat_user_tables.seq_scan` / `idx_scan`, `pg_stat_user_indexes`).
- Seeding: `generate_series` https://www.postgresql.org/docs/17/functions-srf.html and the bulk
  load advice in https://www.postgresql.org/docs/17/populate.html (load, then `ANALYZE`).
- Subquery semantics behind the `NOT IN` rule: https://www.postgresql.org/docs/17/functions-subquery.html
  (`NOT IN` is NULL-sensitive, so the planner cannot turn it into an anti-join; `NOT EXISTS` can).
- `pg_trgm` (only for the deferred search work): https://www.postgresql.org/docs/17/pgtrgm.html
  (a trusted extension since PostgreSQL 13).

SQLAlchemy 2.0 (`sqlalchemy==2.0.53`, `backend/requirements-ci.txt:24`):

- Relationship loading: https://docs.sqlalchemy.org/en/20/orm/queryguide/relationships.html
  (`selectinload`, `joinedload`, `contains_eager` for a query that already joins, `raiseload` /
  `lazy="raise"` to make an unplanned lazy load fail loudly).
- Engine and connection events: https://docs.sqlalchemy.org/en/20/core/events.html#sqlalchemy.events.ConnectionEvents.before_cursor_execute
  (listen on an `Engine` or a single `Connection`).
- Profiling FAQ: https://docs.sqlalchemy.org/en/20/faq/performance.html (statement logging and
  timing with cursor events is the documented approach).
- Large result sets: https://docs.sqlalchemy.org/en/20/orm/queryguide/api.html#fetching-large-result-sets-with-yield-per
- Compiling a statement with literal values for `EXPLAIN`:
  https://docs.sqlalchemy.org/en/20/faq/sqlexpressions.html#rendering-bound-parameters-inline

Alembic:

- `MigrationContext.autocommit_block()`: https://alembic.sqlalchemy.org/en/latest/api/runtime.html#alembic.runtime.migration.MigrationContext.autocommit_block
- `op.create_index(..., postgresql_concurrently=True)`: https://alembic.sqlalchemy.org/en/latest/ops.html#alembic.operations.Operations.create_index

Library check for query counting. `pytest-django`'s `django_assert_num_queries` is Django only.
`nplusone` detects lazy loads only and its last release is from 2018. SQLAlchemy's
`sqlalchemy.testing.assertsql` is the library's internal test-suite API, not a public contract.
The documented, stable mechanism is the `before_cursor_execute` event, and this repo already
uses it in tests (`backend/tests/unit/test_privacy_purge_opensearch_scope.py:335-357`,
`backend/tests/db_locks.py:114`) and in production (`backend/app/core/db_metrics.py`). The
helper in section 5.4 (a) is a thin, shared wrapper over that event, justified because the
per-request counter in `db_metrics` is registered on the app engine while tests run on a separate
engine (`backend/tests/conftest.py:323`), so it cannot be reused directly.

## 2. What already exists (reuse, do not rebuild)

| Existing | Where | Use here |
|---|---|---|
| Concurrent index builder | `backend/app/db/concurrent_indexes.py` (`IndexSpec`, `ensure_concurrently`: AUTOCOMMIT, `SET lock_timeout`, INVALID leftover dropped, never raises) | Every new index (section 5.2) |
| Post-upgrade index hook | `backend/app/db/migrations.py:269-307` `_retry_concurrent_indexes` (tuple of `(table, specs, gating column)`) | Register the new spec module |
| Index spec modules | `backend/app/db/speaker_indexes.py`, `trash_indexes.py`, `hold_indexes.py`, `object_key_indexes.py` | Shape to copy |
| Detection precedent for a no-DDL index revision | `backend/app/db/schema_detect/governance.py:289-292, 364, 372-376` (`_OWN_RUNG_ONLY`) | Section 5.2 step 5 |
| Migration lock guard | `scripts/guards/check_migration_safety.py` (R1: `CREATE INDEX` without `CONCURRENTLY`; R6: no `lock_timeout`), hook `guard-migration-safety` in `.pre-commit-config.yaml:348` | Must pass with no baseline entry |
| Per-request statement counter + slow log | `backend/app/core/db_metrics.py` (`SLOW_QUERY_MS`, default 500, `backend/app/core/config.py:763`; histogram `db_queries_per_request`; access log field `dbq=`) | Live-stack N+1 signal; the access log `dbq=` is captured during the measurement run |
| Statement recording in a test | `backend/tests/unit/test_privacy_purge_opensearch_scope.py:335-357` | Helper in 5.1 generalises it |
| Structural plan assertion | same file `:360-380` (`Anti Join` present, `SubPlan` absent) | Section 5.2 |
| Index-usability assertion | `backend/tests/unit/test_v458_migration_consistency.py:207-255` (scratch DB, `SET LOCAL enable_seqscan = off`) | Section 5.3 |
| Isolated seeding + live-stack guard | `backend/app/scripts/corpus_injection/env.py` (`bootstrap()`, `guard_live_stack()`), wrapper `scripts/inject-eval-corpus.sh` (fresh port resolution) | The seed tool reuses both |
| Rules for inert seeded rows | docstring of `backend/app/scripts/corpus_injection/injector.py:1-40` (`COMPLETED` status, `completed_at` NULL so no post-transcription sweep fires, no object needed in storage) | Same rules for the seed |
| Scale-test design | `docs/design/multi-user-scale-test_plan.md` (proposes `app.scripts.scale_dataset`, not built; open question 3 on `pg_stat_statements` by default) | The seed module is that package's first, Postgres-only piece |

Not present (checked): any `pg_stat_statements`, `auto_explain` or `log_min_duration_statement`
setting in a compose file; any `pg_trgm` extension or trigram index; any `raiseload` or `lazy=`
on a model relationship (every relationship is the default lazy `select`); the
removed `scripts/test-in-worktree.sh` (see risk R7).

## 3. Hot-path inventory and hypotheses

Every "Suspect" is a **hypothesis to measure**, not a finding. Paths are under `backend/app/`.
"L1".."L5" name the lane that owns the follow-up (section 6).

### 3.1 Gallery list and filters (`GET /api/files`), L1

| # | Path | Query (file:line) | Suspect |
|---|---|---|---|
| G1 | List handler `api/endpoints/files/listing.py:142-308` | Scope: admin `:222-224`, `mine` `:225-232`, `shared`/`all` `MediaFile.id.in_(accessible subquery)` `:233-257`; accessible set is `union_all(owned, shared, via_org_collection)` at `services/permission_service.py:249-309`; `via_org_collection` uses `_org_base_rank`, a correlated scalar subquery per collection row (`services/collection_visibility.py:53-78, 178-199`) | Hash semi-join over the UNION is fine; the per-collection correlated subquery grows with all collections in the install |
| G2 | Total count `listing.py:96` | `filtered_query.with_entities(count(MediaFile.id)).scalar()` before the page query | Every filter and subquery runs twice per request; for an admin or community-edition scope it counts every live row |
| G3 | Sort + page `listing.py:81-106` | `order_by(sort_field)` with no `id` tie-breaker, `.offset((page-1)*page_size)` | Sort on `duration` / `file_size` / `completed_at` has no usable index (`idx_media_file_user_completed` is partial on `status='completed'`); OFFSET is O(offset); unstable order between pages (a correctness finding, fix in L1) |
| G4 | Text search `api/endpoints/files/filtering.py:55-61` | `filename ILIKE '%x%' OR title ILIKE '%x%'` | Leading-wildcard ILIKE, no trigram index: sequential filter over the whole scope. Index fix deferred (6.4); measure only |
| G5 | Tag filter `filtering.py:84-91` | `select(FileTag.media_file_id).join(Tag).where(Tag.name.in_(tag)).group_by(...).having(count(distinct Tag.name) == n)` | Not tenant-bounded: aggregates every user's `file_tag` rows with that name before intersecting with the scope |
| G6 | Speaker filter `filtering.py:113-178` | Correlated `EXISTS(speaker WHERE media_file_id = media_file.id AND (CASE(name_hidden) = s OR name = s))`; `hidden_speaker_name_clause` calls `machine_identity_clauses(view, query.with_entities(MediaFile.id))` (`filtering.py:127-132`) | The privacy scope pre-query (`services/privacy/redact_view.py:417-424`) re-runs the whole base gallery query; when a scope is off, the returned clause nests the base query a second time. The CASE makes `idx_speaker_display_name` (v100) unusable for the name arm |
| G7 | Legal-hold filter `held=true` `filtering.py:456-472` -> `services/hold_guard.py:194-199` | `or_(MediaFile.legal_hold.is_(True), EXISTS(content_hold_file JOIN content_hold WHERE released_at IS NULL))` | EXISTS under OR cannot become a semi-join: a SubPlan per scoped row. `held=false` (`not_held_pred`, AND + NOT EXISTS) is an anti-join and should be fine |
| G8 | `held_count` `filtering.py:700-715` (`func.count(MediaFile.id).filter(held_pred())`, `:711`), served by `GET /files/metadata-filters` `listing.py:311-335` | Same EXISTS under OR, inside an aggregate FILTER, evaluated for every scoped row on every filter-panel load. The neighbouring `array_agg(DISTINCT metadata_important->>...)` (`:678-692`) is a full scope scan with JSONB extraction |
| G9 | Hidden files `services/hidden_media_service.py:70-88`, applied at `listing.py:263` and `filtering.py:659-661` | `NOT EXISTS(user_hidden_media WHERE user_id = u AND media_file_id = media_file.id)` | Anti-join on PK `(user_id, media_file_id)` (v465). Expected fine; measured as a control-like path |
| G10 | Page serialisation `listing.py:111-139` | `PrivacyView(db)` created at `:117`, but `preload_files(...)` is never called for the page | **N+1 candidate**: `format_speaker_name` -> `voiceprints_off_for_row` -> `PrivacyView.file_scope` issues one `SELECT ... FROM media_file WHERE id IN (one id)` per file (`redact_view.py:97-128`). Statement count grows with page size |
| G11 | `GET /files/owners` `listing.py:338-380` -> `filtering.py:516-545` | `User JOIN MediaFile WHERE MediaFile.id IN (accessible) DISTINCT ORDER BY full_name LIMIT 100` | Joins every accessible file before DISTINCT |
| G12 | Transcript-search filter `filtering.py:338-405` | OpenSearch `size: 10000`, then `MediaFile.uuid.in_(<up to 10,000 literals>)` (`:401`) | Large IN list planned twice (count + page); also silently truncates at 10k (record as a correctness note) |
| G13 | Unused-tag listing `services/tag_collisions.py:294-303` | `Tag.id.not_in(select(FileTag.tag_id).where(...))` | **The only `NOT IN (SELECT ...)` outside the privacy code**: hashed SubPlan that degrades past `work_mem`. Rewrite as `~exists()` (L1) |

### 3.2 Trash, legal holds (this release), L1

| # | Path | Query | Suspect |
|---|---|---|---|
| T1 | `GET /files/trash` `api/endpoints/files/trash.py:64-82` -> `services/trash_access.py:119-142` | `deleted_at IS NOT NULL AND org stamp [AND user_id = u]`, `ORDER BY deleted_at DESC, id DESC OFFSET/LIMIT`, total via `query.count()` (subquery over the full SELECT) | Only trash index is `ix_media_file_trash_due (purge_after) WHERE deleted_at IS NOT NULL` (`db/trash_indexes.py:9-14`); the list needs a sort on `deleted_at`. Candidate index (only if measured): `(user_id, deleted_at DESC) WHERE deleted_at IS NOT NULL` |
| T2 | `POST /files/trash/empty` `trash.py:172-191` | `trash_access.purgeable_query(db, ctx).all()` (`:185`) loads every trashed row at full width (`metadata_raw`, `waveform_data` not deferred), then one outbox row per file (`services/trash_service.py:255-264`) | Unbounded ORM load; linear outbox inserts. Fix by `load_only`/deferral and a bulk outbox insert if measured |
| T3 | Purge selector `services/trash_purge.py:65-83` (beat `files.purge_trash`) | `deleted_at IS NOT NULL AND purge_after <= clock_timestamp() AND not_held_pred() ... ORDER BY purge_after, id LIMIT 200` | `clock_timestamp()` is volatile so it is not an index condition; the planner should still walk `ix_media_file_trash_due` in order and stop at LIMIT. Verify with EXPLAIN, expected fine |
| T4 | Admin/org hold lists `api/endpoints/admin_holds.py:90-107`, `org_admin_holds.py:95-106` -> `services/content_hold_queries.py:76-91` -> `services/content_hold_records.py:37-100` | Page query is batched; then `hold.collection` per hold (`content_hold_records.py:82`) | **N+1 candidate**: one `collection` SELECT per hold on a page (up to 200). Fix with `selectinload(ContentHold.collection)` |
| T5 | Hold badge per gallery page `services/content_hold_queries.py:122-134` -> `hold_guard.hold_states` `:217-245` | Two batched queries per page | Expected fine (control) |
| T6 | Admin quarantine list `api/endpoints/admin_quarantine.py:92-104` | Default uses partial `ix_media_file_is_quarantined`; `include_legal_holds=true` ORs in `legal_hold` (no index) | Seq scan only with the flag. Measure; likely acceptable for an admin page |

### 3.3 Search (Postgres side), L1

Search ACL is an OpenSearch terms clause and hits are not hydrated row by row; the Postgres work
is small, page-batched queries. Exceptions:

| # | Path | Query | Suspect |
|---|---|---|---|
| S1 | `collection_permission_ranks` `services/collection_visibility.py:108-116`, reached from `GET /search` via `resolve_collection_filter` (`:365`, `search.py:165`) and `_attach_visible_collections` -> `collections_on_files` (`search_helpers.py:93-111`, `collection_visibility.py:291-336`) | `select(Collection.id, greatest(_org_base_rank(u), _share_rank(u))).where(rank > 0)` | Scans **every** collection in the install with two correlated subqueries per row, once or twice per search request. Rewrite as a UNION of indexed lookups (member orgs' collections, shares to the user, shares to the user's groups) |
| S2 | ACL session per OpenSearch leg `services/search/access_filter.py:58-59` | A fresh `session_scope` + `org_readable_collection_ids` per leg (main, up to 3 `count_matches`, `/count`, `/suggestions`, `/filters`) | Repeated identical queries per request; memoise per request if measured |
| S3 | Collection filter `services/search/filters.py:21-48` | Up to 65,536 uuids pulled from Postgres per leg | Measure only; a later design question (6.4) |

### 3.4 Speaker directory and profile counts, L2

| # | Path | Query | Suspect |
|---|---|---|---|
| P1 | `GET /speakers` full list (`for_filter=False`) `api/endpoints/speakers_list.py:374-399` | `db.query(Speaker).options(joinedload(profile), joinedload(media_file))` filtered by `user_id` and scope; **no LIMIT**, sorted in Python (`:400`); `~Speaker.media_file_id.in_(select(MediaFile.id).where(unavailable_pred()))` at `:396-397` | **`NOT IN` subquery** (hashed SubPlan, degrades past `work_mem`), plus an unpaged fetch of every owned speaker. Rewrite the exclusion as a join on `available_pred()` or `NOT EXISTS`. Pagination is an API change: out of v0.7.0 unless the frontend already pages (check the caller first) |
| P2 | Filter roster `GET /speakers?for_filter=true` `speakers_list.py:348-367` -> `api/endpoints/speakers_list_filters.py:107-260` | Same `NOT IN` at `:213-214`; `label_col.ilike('%q%')` `:219`; `GROUP BY` a CASE/COALESCE label with `count(DISTINCT media_file_id)`, `ORDER BY` aggregates, `LIMIT n` (`:228-244`) | Reads every in-scope speaker before LIMIT; `NOT IN` as P1; CASE label is not indexable (by design of the privacy rule). Fix the `NOT IN`; measure the rest |
| P3 | `machine_identity_clauses` `services/privacy/redact_view.py:332-372`, helper `_in_off_file` `:397-446` | Scope pre-query `SELECT DISTINCT user_id, organization_id FROM media_file WHERE id IN (<scope>) AND id IN (SELECT media_file_id FROM speaker WHERE identity_source IN ('voiceprint','voiceprint_link'))` | This is #1328's 170 ms. The inner subquery matches the v458 partial index predicate exactly. Callers' main queries use the clauses in CASE or negated form, which the partial index cannot serve (`speakers_list_filters.py:196`, `speaker_profiles.py:81`, `files/filtering.py:129`, `services/chat/speaker_roster.py:236`). Measure index present vs absent (L0) |
| P4 | `hidden_suggestion_clause` `redact_view.py:375-394`, chat roster `services/chat/speaker_roster.py:236-237` | `_in_off_file` with `suggestion_source IN (...)`: no index on `suggestion_source`, not bounded by file | Whole-table scan of `speaker` per chat turn. Candidate partial index `(media_file_id) WHERE suggestion_source IN (...)` only if measured (L5) |
| P5 | `GET /speaker-profiles/profiles` `api/endpoints/speaker_profiles.py:163-257`, counts `_profile_media_instance_counts` `:63-101`, access `services/permission_service.py:478-501` | One GROUP BY for counts (good); the shared-profile branch is a `DISTINCT` over `speaker_profile -> speaker -> media_file -> collection_member -> collection_share`; `profile_id IN (<python list>)`; no pagination | DISTINCT fan-out over every speaker of every shared file; for a see-all caller `profile_id IN` covers most of `speaker` |
| P6 | Profile occurrences `GET /speaker-profiles/profiles/{uuid}/occurrences` -> `services/speaker_matching/profiles.py:88-140` | `query(Speaker).join(MediaFile)` without `contains_eager`; loop reads `speaker.media_file` (`:138`) and calls `view.voiceprints(file_id)` (`:132-133`) without `preload_files` | **N+1**: one `media_file` SELECT plus one scope SELECT per distinct file |
| P7 | Cross-media `GET /speakers/{uuid}/cross-media` `api/endpoints/speakers_occurrences.py:115-145, 177-200` | Same join without `contains_eager`; loops at `:135-138` and `:194-197` read `speaker.media_file`; the name branch filters `display_name = x` with no `user_id` for a see-all caller | **N+1** as P6; unbounded result. `idx_speaker_display_name` (v100) exists, so the name branch is expected to be indexed: verify |
| P8 | Cluster inbox `GET /speaker-clusters/unverified/inbox` `services/clustering/verification.py:69-99` | `user_id = u AND verified IS FALSE AND profile_id IS NULL`, outer join cluster, `ORDER BY member_count DESC NULLS LAST, confidence, created_at DESC`, `count()` + OFFSET | No `(user_id, verified, profile_id)` index; sort over all unverified speakers of the user. Candidate partial index only if measured |
| P9 | Cluster list `services/clustering/queries.py:33-152` and stats `api/endpoints/speaker_clusters.py:323-343` | Four separate `count()` + `max(updated_at)` + OFFSET page; stats runs two counts over the user's speakers | Repeated scans per request; combine with `count(*) FILTER (...)` in one statement if measured |
| P10 | Startup profile sync `services/opensearch_service/speaker_metadata.py:226-266`, called from `main.py:193-197` on every API start | `db.query(Speaker).filter(profile_id IS NOT NULL OR display_name IS NOT NULL).all()`, then per row an OpenSearch `exists` and lazy `speaker.profile` | Up to 1M ORM rows, 1M OpenSearch HEADs and one profile SELECT per distinct profile, on the startup path. Measure the startup time on the seeded stack; fix (chunked `yield_per`, `selectinload(profile)`, OpenSearch `mget`) is L2 if measured slow |
| P11 | Identity history `services/speaker_identity_history.py:177-182` | `speaker_id = x ORDER BY created_at DESC, id DESC LIMIT n` | Served by `ix_speaker_identity_event_speaker_created`. Control |

### 3.5 Task listing, L3

| # | Path | Query | Suspect |
|---|---|---|---|
| K1 | `GET /tasks` `api/endpoints/tasks.py:292-339` | `_get_user_media_files` (`:70-102`) loads **every** visible file (`query.all()`, no LIMIT); `_latest_task_by_file` (`:105-153`) loads **every** task row of those files ordered by `(media_file_id, created_at)`; filtering, total and paging happen in Python (`:320-339`) | The largest single finding: at 100k files and ~300k tasks every page load reads both tables in full. Rewrite with SQL filtering, `DISTINCT ON (media_file_id)` or a `LATERAL` latest-task join, SQL `count` and `LIMIT/OFFSET`, keeping the response shape |
| K2 | `GET /tasks/system/health` `api/endpoints/tasks_system_health.py:54-112` | Unfiltered aggregates over `task` and `media_file`; loops read `task.media_file.uuid` (`:93`) and `file.user.uuid` (`:112`) | **N+1** on both loops; full scans (admin page, acceptable if under budget) |
| K3 | Recovery `api/endpoints/tasks_recovery.py:57-65, 273` | Lazy `task.media_file` in a loop | **N+1** |
| K4 | `get_recent_tasks` `utils/stats_helpers.py:225-236` | `order_by(Task.created_at.desc()).limit(n)` | No `task.created_at` index: seq scan + top-N sort. Candidate index (L5) if measured |

### 3.6 Admin dashboards, L3

| # | Path | Query | Suspect |
|---|---|---|---|
| A1 | `GET /admin/stats` `api/endpoints/admin.py:67-155` | `async def` handler running synchronous `db.query` (`get_user_stats`, `get_file_stats` `utils/stats_helpers.py:110-137` including `count(transcript_segment.id)` and `count(speaker.id)`, `get_task_stats` `:173-186`) | Blocks the event loop for the whole request (correctness and latency finding: make it a plain `def` so FastAPI runs it in the threadpool); several full scans; the comment "cheap, indexes exist" at `:135` is untrue for a 10M-row count |
| A2 | `GET /system/stats` `api/endpoints/system.py:135` | Same helpers plus `get_throughput_stats` (`stats_helpers.py:282-292`), `get_processing_eta` (`:326-342`), `get_file_timing_stats` (`:370-385`) | 7+ full scans per poll; no standalone `completed_at` index |
| A3 | `GET /admin/users` `admin.py:255-271` | `ORDER BY lower(full_name) OFFSET/LIMIT` | Small table; low priority, measure only |
| A4 | `GET /admin/timing` `api/endpoints/admin_timing.py:83-94` | `user_id = u ORDER BY created_at DESC LIMIT n` | Separate single-column indexes; composite `(user_id, created_at)` only if measured |
| A5 | Storage integrity `api/endpoints/admin_storage_integrity.py:86-110`, `tasks/opensearch_integrity_task.py:62-94`, `db/object_key_indexes.py:102-110` | Loads all `media_file.id`/`uuid` and 1M `speaker.uuid` into Python; three GROUP BY passes | Admin-triggered and every 6 h on beat; measure memory and time |

Audit logs are stored in OpenSearch (`auth/audit_query.py:78`), so their deep `from` paging is out
of scope for a Postgres review (6.4).

### 3.7 Privacy, purge and periodic scans, L4

| # | Path | Query | Suspect |
|---|---|---|---|
| V1 | Voiceprint purge `services/privacy/purge_voiceprints.py:653-666` and `services/privacy/purge_residual.py:135-142` | `delete(SpeakerMatch).where(or_(speaker1_id.in_(in_scope), speaker2_id.in_(in_scope)))` | **OR of two IN subqueries**: cannot be pulled up into a semi-join; a SubPlan per arm, non-hashed when the scope (up to 1M ids) exceeds `work_mem`. The existing test only rejects the literal `NOT IN (SELECT`. Rewrite as two DELETEs (one per arm) or `id IN (SELECT ... UNION SELECT ...)` |
| V2 | Cluster candidates `purge_voiceprints.py:402-500` | Same OR-of-IN shape over `speaker_cluster`; then 5,000-id chunks that each re-evaluate the 1M-row scope subquery (`:475`, `:481`) | As V1, plus O(chunks x scope) |
| V3 | `_revert_machine_identities` `purge_voiceprints.py:213-261`, `purge_voiceprints_postgres` `:556-609` | Four `COUNT(*)` + two `UPDATE ... RETURNING`, each re-running the scope semi-join; whole-scope uuid lists materialised in Python | Six scope scans that could be fewer; unbounded lists |
| V4 | `record_bulk_identity_events` `services/speaker_identity_history.py:70-82` | One INSERT with `ANY(:ids)` of every reverted speaker | Up to 1M-element array parameter in one statement; chunk if measured |
| V5 | `refresh_cleared_speakers` `services/privacy/purge.py:378-395` | Per 500-id batch, loads ORM rows and updates `computed_status` one row at a time | 1M single-row UPDATEs for an instance purge; set-based UPDATE if `compute_speaker_status` can be expressed in SQL, else `bulk_update_mappings` |
| V6 | Residual measurement `services/privacy/purge_residual.py:77-168` | Nine COUNTs each re-evaluating `scope.speaker_filters()`; `suggestion_detail->>'origin'` unindexed (`:111`) | Combine into one pass with `count(*) FILTER (...)` |
| V7 | `scrub_chat_citations` `services/file_cleanup_service.py:178-195`, once per purged file (`:425`) | `UPDATE chat_message ... WHERE jsonb_typeof(citations)='array' AND EXISTS (SELECT 1 FROM jsonb_array_elements(citations) e WHERE e->>'file_uuid' = ANY(:uuids))` | Full scan of `chat_message` with JSONB expansion **per destroyed file**: O(files x messages) for retention, trash, GDPR and org erasure. Fix: batch the uuids across a purge run (one scan per batch). A GIN/extracted-column index is a later schema change |
| V8 | ORM cascade on `db.delete(MediaFile)` `services/file_cleanup_service.py:426`; relationships `models/media.py:518-545` | `cascade="all, delete-orphan"` without `passive_deletes`; `transcript_segment.media_file_id` FK has no `ON DELETE CASCADE` in the baseline (`alembic/versions/v010_baseline.py:354`) | Per file: ~9 SELECTs plus one DELETE per child row. Count statements per purged file in L4 (measure only); the FK change is later (6.4) |
| V9 | GDPR erasure `services/gdpr_erasure_service.py:247, 428, 600` -> `gdpr_erasure_steps.py:94-112` | Loads every file of a user or org as full ORM rows, then loops `purge_media_file`; `_purge_files` callers do not `selectinload(speakers)` | Memory and N x V8 x V7. Chunk by id with `yield_per` / batches |
| V10 | Embedding consistency (beat, every 10 min) `tasks/speaker_embedding_consistency.py:53-66, 237-243` | `Speaker.uuid, media_file_id WHERE EXISTS(transcript_segment WHERE speaker_id = speaker.id)`, then every `speaker.uuid` | ~2M rows into Python every 10 minutes; heaviest periodic job |
| V11 | Search-index sweep (beat, every 10 min) `services/search/index_retry.py:89-158`; `count_files_awaiting_index` also called from `core/backup_metrics.py:203` | `status=COMPLETED AND EXISTS(segment) AND (latest='failed' OR ...) ORDER BY id LIMIT n`, `latest` a correlated `ORDER BY ... LIMIT 1` subquery on `task` | When matches are sparse, walks all completed files with two probes each per tick |
| V12 | Label drift scan `services/privacy/label_reindex.py:137-173` (from search-index maintenance) | `EXISTS(speaker WHERE media_file_id = media_file.id AND (identity_source IN (...) OR suggestion_source IN (...)))`, keyset by `id` | The OR arm on `suggestion_source` defeats the partial index |
| V13 | Retention `tasks/cleanup.py:486-499` (`cleanup_expired_files`, hourly when enabled) | `(completed_at IS NOT NULL AND completed_at < cutoff) OR (completed_at IS NULL AND upload_time < cutoff)`, no LIMIT | OR across two columns, no `completed_at` index: seq scan; unbounded candidate list |
| V14 | Voiceprint retention `tasks/voiceprint_retention.py:94-145` | Candidate query `LIMIT 800`, then `_has_voiceprints` per candidate (`:117-127`): one SQL + one OpenSearch count each | **N+1**; and the hypothesis that purged files keep their speaker rows and come back every day (non-convergence) must be checked: if true it is a correctness bug, fixed in L4, not deferred |
| V15 | Deep cleanup / stuck files `services/file_cleanup_cycle.py:137-152, 235-276, 312-345`; `utils/stuck_file_recovery.py:45-103` | Unbounded `.all()` of ORPHANED/ERROR files; per-file commit in loop | Small sets on a healthy install; measure only |
| V16 | Chat retention `tasks/chat_retention.py:53-75` | `last_message_at < cutoff`, no index; ORM `db.delete` per conversation | Measure; candidate index only if measured |
| V17 | `not_in_scope` `services/privacy/purge_voiceprints.py:169` | Already `NOT EXISTS`; plan asserted `Anti Join` by `test_privacy_purge_opensearch_scope.py:360-380` | Control: must stay green |

### 3.8 Other N+1 loops found (small or bounded; fix when the owning lane touches the file)

- `services/permission_service.py:350-361`: one `user_group_member` query per group share of a file (L1).
- `services/tag_operations.py:567-585`: per selected tag, three lookups (bounded by the request; L1, only if trivially batched).
- `services/clustering/recluster.py:81-89, 149-155`: per old cluster, an UPDATE + cascade delete, and a member query per cluster (L2, measure first).
- `services/analytics_service.py:207`: lazy `segment.speaker` per segment (identity map bounds it to distinct speakers; L3, low).
- `services/gdpr_erasure_steps.py:44-50, 191-200`: `db.delete` per profile / collection / conversation with cascade loads (L4).
- `api/endpoints/files/bulk_action_handlers.py:450-492`: bulk hide does one permission check, lock, count and commit per file (L1, measure).
- `services/speaker_status_service.py:201-216`: lazy `speaker.profile` per row in `compute_status_for_speakers`, which has no callers. Confirm with `rg` and delete it rather than optimise (house rule: no second path doing the same job).

## 4. Measurement procedure

### 4.1 Stack, extensions, logging

All commands from the lane worktree root. Never against the live dev stack: the seed tool's
guard refuses ports 5176/5178/5180 (`corpus_injection/env.py:105-120`).

1. Check host load first (shared host, see the owner's load budget): no other full suite, gate or
   GPU batch running. Record `uptime` in the run's `env.txt`.
2. Start an isolated stack:
   ```bash
   ./opentr.sh fresh-list                                   # pick an unused offset
   ./opentr.sh start dev --fresh perf1337 --port-offset 300 --dry-run
   ./opentr.sh start dev --fresh perf1337 --port-offset 300
   ```
   The offset is remembered in `.fresh/perf1337.offset`. Postgres is then on host port
   `5176 + 300 = 5476`.
3. Find the Postgres container by compose project label, never by a name grep:
   ```bash
   PG=$(docker ps -q --filter label=com.docker.compose.project=otfresh-perf1337 \
                     --filter label=com.docker.compose.service=postgres)
   psqlf() { docker exec -i "$PG" psql -U postgres -d opentranscribe -v ON_ERROR_STOP=1 "$@"; }
   ```
   (`POSTGRES_USER` / `POSTGRES_DB` default to `postgres` / `opentranscribe`, `.env.example:45-46`;
   use the values the fresh stack actually runs with.)
4. Enable the extensions on this stack only (writes `postgresql.auto.conf` inside the fresh
   `otfresh-perf1337_postgres_data` volume; the live stack is untouched):
   ```sql
   ALTER SYSTEM SET shared_preload_libraries = 'pg_stat_statements,auto_explain';
   ALTER SYSTEM SET pg_stat_statements.track = 'top';
   ALTER SYSTEM SET pg_stat_statements.max = 10000;
   ALTER SYSTEM SET track_io_timing = on;
   ALTER SYSTEM SET log_min_duration_statement = '100ms';
   ALTER SYSTEM SET auto_explain.log_min_duration = '100ms';
   ALTER SYSTEM SET auto_explain.log_analyze = on;
   ALTER SYSTEM SET auto_explain.log_buffers = on;
   ALTER SYSTEM SET auto_explain.log_timing = on;
   ALTER SYSTEM SET auto_explain.log_nested_statements = on;
   ALTER SYSTEM SET auto_explain.log_format = 'json';
   ```
   Restart through the script: `./opentr.sh stop --fresh perf1337` then
   `./opentr.sh start dev --fresh perf1337`. Then:
   ```sql
   CREATE EXTENSION IF NOT EXISTS pg_stat_statements;
   SELECT name, setting, source FROM pg_settings
    WHERE name IN ('shared_preload_libraries','track_io_timing','auto_explain.log_min_duration',
                   'shared_buffers','work_mem','random_page_cost');
   ```
   Preflight: if `shared_preload_libraries` does not show both libraries, stop and report; do not
   edit `docker-compose.yml` in this issue (the compose knob is the scale plan's open question 3).
   Keep the stack's tuning values (`shared_buffers=256MB`, `work_mem=16MB`, ...) unchanged: the
   measurement must reflect the shipped defaults.

### 4.2 Seed the dataset

New tool (lane L0): `backend/app/scripts/scale_dataset/` with `__main__.py` and `pg_synthetic.py`,
named to become the first piece of the package `docs/design/multi-user-scale-test_plan.md` section
7 proposes. Postgres only: no OpenSearch documents, no MinIO objects (the injector docstring
proves nothing validates `storage_path`).

- Bootstrap and guard: call `corpus_injection.env.bootstrap()` and `guard_live_stack()` first,
  exactly as the injector does. Wrapper `scripts/seed-query-review.sh --fresh perf1337` resolves
  the offset ports the way `scripts/inject-eval-corpus.sh` does and runs it from `backend/venv`.
- Set-based inserts, never ORM row-by-row: `INSERT INTO <table> (<cols>) SELECT ... FROM
  generate_series(1, N) g` built with SQLAlchemy Core `insert(Model).from_select(...)` so column
  names come from the models and schema drift fails loudly. FK order, one transaction per table,
  `ANALYZE` at the end (PostgreSQL "Populating a Database"). Values derived deterministically from
  `g` (`hashtext(g::text)`, modulo arithmetic), never `random()`, so two seeds are identical.
- Marks: users `perf-<n>@loadtest.invalid`; `media_file.metadata_important.loadtest = '<seed id>'`.
- Inert rows (copy the injector's verified rules): `status = 'completed'`, `completed_at` NULL,
  no `PROCESSING`/`PENDING` rows. Periodic jobs that still select seeded rows (V10-V13) are part of
  what is being measured.
- Default shape (flags override each count; the issue's two numbers are fixed):

  | Table | Rows | Distribution notes |
  |---|---|---|
  | `organization` | 5 | plus personal workspace (org NULL) |
  | `user` | 1,000 | one heavy user with 20,000 files; one admin |
  | `media_file` | 100,000 | 2% trashed (`deleted_at`, `purge_after`, `trash_origin='user'`), 0.5% quarantined, 0.2% `legal_hold`, spread `upload_time` over 12 months, realistic `duration`, `file_size`, `content_type`, `metadata_important` format/codec keys |
  | `speaker` | 1,000,000 | 10 per file; `identity_source`: 5% `voiceprint`, 2% `voiceprint_link`, rest human/NULL; 30% `display_name` set; `suggestion_source` set on 3%; 40% linked to a profile; 60% unverified |
  | `speaker_profile` | 20,000 | |
  | `speaker_cluster` / `speaker_cluster_member` | 50,000 / 500,000 | |
  | `speaker_match` | 500,000 | |
  | `transcript_segment` | 10,000,000 | 100 per file, short text; flag `--segments-per-file` to shrink for a quick run |
  | `task` | 300,000 | 3 per file, terminal statuses only |
  | `tag` / `file_tag` | 2,000 / 300,000 | |
  | `collection` / `collection_member` / `collection_share` | 5,000 / 200,000 / 2,000 | |
  | `content_hold` / `content_hold_file` | 50 / 2,000 | mixed authority, some released |
  | `user_hidden_media` | 5,000 | |
  | `chat_conversation` / `chat_message` | 20,000 / 200,000 | `citations` JSON arrays with `file_uuid`s (for V7) |

  Read each model at implementation time for NOT NULL columns and CHECK constraints
  (`ck_media_file_trash_state`, `ck_content_hold_authority_org`, ...); the table above is the
  target distribution, not the column list.
- Snapshot after seeding and `ANALYZE`, so every before/after run starts from identical data:
  ```bash
  docker exec "$PG" pg_dump -U postgres -Fc -d opentranscribe > ~/ot-cleanup-work/1337/perf/seed-<seed>.dump
  ```
  Restore for a run: `docker exec -i "$PG" pg_restore -U postgres -d opentranscribe --clean --if-exists < ...`
  then `ANALYZE`. Record the dump's sha256 in the run manifest.

### 4.3 Capture

Artifacts live outside the repo (they contain only synthetic data, but they are large), in
`~/ot-cleanup-work/1337/perf/<run-id>/` (not `/tmp`, which is cleaned on this host). `<run-id>` =
`<date>-<branch>-<short sha>-<label>`. Each run directory holds `env.txt` (git sha, pg settings
query output, `uptime`, seed dump sha256), `pgss.csv`, `tables.csv`, `explain/<name>.json`,
`auto_explain.log` (from `docker logs` of the fresh Postgres container), `access.log` (backend
access log lines with `dbq=`), and `summary.md`.

1. Reset counters: `SELECT pg_stat_statements_reset(); SELECT pg_stat_reset();`
2. Drive the endpoints with a deterministic driver (lane L0: `scripts/perf/query-review/drive.py`,
   stdlib + `httpx` from the backend venv). It logs in as three seeded identities (the heavy user,
   an org member, the admin) and calls, 20 times each: `GET /api/files` (default, `page=50`,
   `search=`, `tag=`, `speaker=`, `held=true`, `held=false`, `hidden=only`, `sort_by` each of
   upload_time/duration/file_size/completed_at), `GET /api/files/metadata-filters`,
   `GET /api/files/owners`, `GET /api/files/trash`, `GET /api/search?q=...` (Postgres legs only;
   OpenSearch may be empty), `GET /api/speakers`, `GET /api/speakers?for_filter=true&q=`,
   `GET /api/speaker-profiles/profiles`, one profile's `/occurrences`, one speaker's `/cross-media`,
   `GET /api/speaker-clusters`, `/unverified/inbox`, `/stats`, `GET /api/tasks`,
   `GET /api/tasks/system/health`, `GET /api/admin/stats`, `GET /api/system/stats`,
   `GET /api/admin/holds`, `GET /api/admin/files/quarantined`. Resolve real route paths from
   `backend/app/api/router.py` at implementation time. It writes per-call wall time and the
   access-log `dbq` count.
3. Trigger the periodic jobs once each through their Celery task names (embedding consistency,
   search-index sweep, search-index maintenance, purge trash, retention with a test policy) and a
   voiceprint purge of one organization scope on the snapshot (restore afterwards).
4. Export:
   ```sql
   \copy (SELECT queryid, calls, total_exec_time, mean_exec_time, stddev_exec_time, rows,
                 shared_blks_hit, shared_blks_read, temp_blks_written, left(query, 500) AS query
          FROM pg_stat_statements ORDER BY total_exec_time DESC LIMIT 100) TO STDOUT WITH CSV HEADER
   \copy (SELECT relname, seq_scan, seq_tup_read, idx_scan, n_live_tup
          FROM pg_stat_user_tables ORDER BY seq_tup_read DESC) TO STDOUT WITH CSV HEADER
   ```
5. For each statement in the top 25 by `total_exec_time`, and for every hypothesis row in section
   3 whether or not it ranked, store a replayable SQL file in
   `scripts/perf/query-review/queries/<id>.sql` (committed; ids match section 3, e.g. `G7.sql`),
   with literal values copied from the auto_explain log or rendered via SQLAlchemy
   `compile(compile_kwargs={"literal_binds": True})`. Then capture:
   ```sql
   EXPLAIN (ANALYZE, BUFFERS, SETTINGS, FORMAT JSON) <statement>;
   ```
   Write statements (V1-V7, V13) run as `BEGIN; EXPLAIN (ANALYZE, BUFFERS, ...) <stmt>; ROLLBACK;`.
   For statements whose shape depends on `completed_at` (A2, V13), set realistic values inside the
   same rolled-back transaction first (`UPDATE media_file SET completed_at = upload_time +
   interval '5 min' WHERE ...;`), so the seeded rows stay inert. `scripts/perf/query-review/explain.sh <run-id> [ids...]`
   runs one warm-up (discarded) and five measured executions per query and records the median
   `Execution Time`, shared hit/read buffers, and the top plan node types.
6. `summary.md`: one row per id: median ms, buffers, plan shape (`Seq Scan on X`, `SubPlan`,
   `Anti Join`, index names used), `dbq` per endpoint call, verdict (`confirmed` / `refuted` /
   `below budget`).

### 4.4 Before/after with a control

- **Same data**: every comparison restores the same seed dump and runs `ANALYZE` first.
- **Same settings**: diff `env.txt` settings between the two runs; any difference voids the pair.
- **Control queries** measured in every run, unaffected by any planned change:
  `C1.sql` = `SELECT * FROM media_file WHERE uuid = '<fixed uuid>'` (PK-like lookup),
  `C2.sql` = the hidden-files anti-join (G9), `C3.sql` = identity history (P11). If a control's
  median moves more than 15% between runs, the pair is noise: re-run both on a quiet host.
- **Index changes**: on the restored snapshot measure with the index absent, then build it with
  the same `IndexSpec` through `ensure_concurrently` (or the runner on restart), `ANALYZE`, and
  measure again. For #1328: `DROP INDEX ix_speaker_machine_identity_media_file` on the fresh stack,
  measure P3, restart via `./opentr.sh stop/start --fresh perf1337` (the runner rebuilds it),
  measure again.
- **Query rewrites**: run old SQL file and new SQL file back to back on the same snapshot (both
  committed as `<id>.sql` and `<id>.after.sql`), and the endpoint driver on the base branch and the
  lane branch (the fresh stack bind-mounts the checkout it is started from; restart the backend
  with `./opentr.sh restart-backend` only if it supports `--fresh`, otherwise stop/start).
- **Evidence that the run measured the new code**: the driver prints the git sha the backend
  reports (or a marker in the access log); the `dbq` count for an N+1 fix must change.
- Record the before/after table in section 9 of this file (the implementer appends it), and the
  #1328 numbers also in that issue when it is closed by the coordinator.

## 5. Decision rules

### 5.1 When an index is warranted (measured, never guessed)

All of:

1. A captured plan for a hot statement shows a `Seq Scan` (or a `Bitmap Heap Scan` with large
   `Rows Removed by Filter`) on a table above 100k rows, **and** the statement is in the top 25 by
   `total_exec_time` or its endpoint misses the budget: median above 100 ms for a page read at
   seed scale (the scale plan's proposed SLO is p95 300 ms end to end).
2. No existing index serves it. Check `pg_indexes` on the fresh stack, not the model file
   (example: `idx_speaker_display_name` exists from v100 although a reader of the model would not
   see it; `media_file.filename` has `index=True` in the model at `models/media.py:68` but no
   migration was found creating it: confirm in `pg_indexes`).
3. A rewrite cannot remove the need (5.3 first).
4. Built on the snapshot, it moves the median by at least 2x **or** brings the endpoint under
   budget, and the plan uses it.
5. Write cost is acceptable: prefer a partial index whose predicate matches the query's own filter
   (the planner must be able to prove implication, PostgreSQL "Partial Indexes"); for a hot-write
   table (`transcript_segment`, `task`, `speaker`) note the extra index size from
   `pg_relation_size` in the summary.

### 5.2 Migration shape (repo precedent: v458, v463, v467)

1. Next number: run `python3 scripts/release-tests/lib/alembic-head.py backend` at implementation
   time and check the lane table in `backend/alembic/CLAUDE.md`. At writing: head
   `v467_add_content_hold_authority`, `v466` reserved for the organization-owner lane, so the next
   free number is `v469` (`v468_add_organization_owner` belongs to data-safety lane L7) **unless another lane has taken it since**. Add a row for this lane to that
   table in the same commit as the revision. Never reuse a number that reached any database.
2. One revision for the whole wave's measured indexes, e.g. `v469_query_review_indexes`. It runs
   **no DDL** in `upgrade()` (module docstring copies v458's reasoning: `CREATE INDEX CONCURRENTLY`
   waits for every transaction that can see the table, so building it inside the startup upgrade
   could hang startup). `downgrade()` runs `DROP INDEX IF EXISTS <name>` per index (destroys no data).
3. Definitions in a new `backend/app/db/perf_indexes.py` (`IndexSpec` constants + a tuple), one
   comment line per index naming the measured statement id from section 3. Register it in
   `migrations._retry_concurrent_indexes` (`backend/app/db/migrations.py:286-299`) with a gating
   column per table, as the three existing entries do. Mirror each index in the model's
   `__table_args__` (as `models/speaker.py:247-251` does for v458).
4. If an index needs a non-btree method or an expression (`lower(...)`, `gin_trgm_ops`),
   `IndexSpec` cannot express it today: extend `IndexSpec` with an optional `using` and raw
   expression list and a unit test, or defer the index (6.4). Do not hand-write a second builder.
5. Detection: add a probe in the newest era module (`backend/app/db/schema_detect/data_safety.py`
   at writing) keyed on one of the revision's indexes, an arm at the top of its `ARMS`, mark the
   probe own-rung-only (the `_OWN_RUNG_ONLY` treatment in `governance.py:372-376`), and add the
   step to `_MODERN_CHAIN` in `backend/tests/unit/test_schema_detection_ladder.py`. Stamping low
   while the index is pending is harmless because the revision has no DDL.
6. Consistency test modelled on `backend/tests/unit/test_v458_migration_consistency.py`:
   `down_revision`, vendor neutrality, `assert_detected_at_or_after`, the build through
   `ensure_concurrently` in a private scratch database (outcome `built`, then `exists`), an
   index-usability EXPLAIN (5.4 c), and downgrade idempotence. DDL tests take
   `acquire_ddl_lock_exclusive` on the shared DB and carry `@pytest.mark.ddl_exclusive` on the
   test, never the module.
7. `python3 scripts/guards/check_migration_safety.py` must pass with **no** new baseline entry.
8. Alembic's documented alternative, `op.get_context().autocommit_block()` with
   `op.create_index(..., postgresql_concurrently=True)`, was considered and rejected for the same
   reason v458 gives: the startup runner holds `pg_advisory_lock(42)` across `command.upgrade()`
   and a concurrent build there blocks startup behind any open transaction. The runner path
   retries on every start and never raises.

### 5.3 Rewrite or change a loader instead of adding an index

| Shape found | Change | Doc |
|---|---|---|
| `col NOT IN (SELECT ...)` (G13, P1, P2) | `~exists().where(...)` or join on the positive predicate | PostgreSQL subquery expressions |
| `OR` of `IN (subquery)` / `EXISTS` under `OR` (V1, V2, G7, G8, V12) | Split into one statement per arm, or `id IN (SELECT ... UNION SELECT ...)`; for `held_pred` add a set-returning `held_file_ids()` form for filters and keep the boolean for row checks | using-explain (SubPlan) |
| Lazy relationship read in a loop (G10, T4, P6, P7, K2, K3, P10) | `selectinload` for collections and many-to-one on a separate page, `contains_eager` when the query already joins the target, `PrivacyView.preload_files(page_ids)` for the privacy view | SQLAlchemy relationship loading |
| Per-row correlated subquery in SELECT/WHERE over a large set (S1, V11, K1 latest task) | `JOIN LATERAL`, `DISTINCT ON`, or a UNION of indexed lookups | |
| Python-side filtering/paging of a full table (K1, P1) | SQL `WHERE`, `count`, `LIMIT/OFFSET`; preserve response shape and ordering exactly | |
| Several COUNTs over the same scope (P9, V3, V6) | One statement with `count(*) FILTER (WHERE ...)` | PostgreSQL aggregate FILTER |
| Unbounded `.all()` over a large table in a job (V9, V10, A5, P10) | `yield_per` or id-keyset batches | SQLAlchemy yield_per |
| One statement per destroyed file scanning a large table (V7) | Batch the file set per purge run | |

An index is added on top of a rewrite only when the rewritten statement still meets 5.1.

**`raiseload`**: use it in tests to prove a list query loads nothing lazily
(`query.options(..., raiseload("*"))` in a test-only wrapper is not possible without touching the
code path, so the statement-count assertion is the regression check). Shipping `raiseload("*")` in
production list queries turns a missed eager load into a 500; that is a later decision (6.4).

### 5.4 Regression checks

**(a) Statement counts that do not grow with rows** (N+1 fixes). New helper
`backend/tests/query_budget.py`:

```python
@contextmanager
def count_statements(*targets: Engine | Connection) -> Iterator[StatementLog]:
    """Record every statement executed on ``targets`` (SQLAlchemy before_cursor_execute)."""
```

- Listens on the `db_session` connection **and** the app engine (`app.db.base.engine`), because
  about 20 app paths open their own `SessionLocal()` during a request (`tests/conftest.py:603-608`)
  and would otherwise escape the count.
- Ignores the harness's own `SAVEPOINT` / `RELEASE SAVEPOINT` / `ROLLBACK TO SAVEPOINT` statements
  and advisory-lock calls from `tests/db_locks.py`.
- `StatementLog` exposes `count`, `matching(regex)` and the list, for readable failure messages.
- Self-test `backend/tests/unit/test_query_budget_helper.py`: must-fire (a deliberate lazy loop
  over 5 rows counts 5 more statements than over 1 row) and must-stay-clean (a `selectinload`
  query counts the same for 1 and 5 rows), per the auditor rule in `backend/CLAUDE.md`'s test
  section.
- Assertion pattern, per endpoint: seed `n=2` and `n=12` rows of the driving entity in
  `db_session`, call the endpoint through `client` for each, assert
  `log12.count == log2.count` (plus a control assertion that the response actually returned 2 and
  12 items, so an empty result cannot pass). Equality of counts across sizes is what stays
  meaningful on a tiny CI database; an absolute ceiling (`<= k`) is added only as a second line
  and documented as a ceiling that moves when a legitimate query is added.

**(b) Structural plan assertions** (rewrites). Compile the real statement the code builds (call the
service function that returns the `Select`, or record it with the helper), `EXPLAIN (FORMAT JSON)`
it on the shared test DB inside the test's savepoint, and assert node types: `Anti Join` / `Semi
Join` present, no `SubPlan`, no `NOT IN (SELECT` in any recorded statement. These do not depend on
table size or `enable_seqscan`, so they hold on CI. Precedent: `test_privacy_purge_opensearch_scope.py:360-380`.

**(c) Index-usability assertions** (new indexes). In a private scratch database (pattern
`test_v458_migration_consistency.py`): create the minimal table, seed tens of thousands of rows
with `generate_series`, build the index with the real `IndexSpec` through `ensure_concurrently`,
`ANALYZE`, then inside a transaction `SET LOCAL enable_seqscan = off` and assert the index name
appears in `EXPLAIN` for each query shape the index exists for, and does not appear for a shape
outside its predicate. Document in the test docstring what this proves and what it does not: it
proves the index is **usable** for that shape; it does not prove the planner **chooses** it at
production size, which only the section 4 measurement shows. `enable_seqscan = off` is a cost
penalty, not a ban, so a plan that still shows `Seq Scan` under it means the index cannot serve the
shape at all.

Trade-offs, stated in each test file's docstring: count assertions are brittle to legitimate new
queries (equality across sizes is robust, absolute ceilings are not); plan assertions on the shared
DB are sensitive to planner version (pin to node types, never to costs or row estimates);
scratch-database tests are slower and take the DDL lock exclusively, so keep them few.

**Hottest endpoints that get a check in v0.7.0** (minimum set): `GET /api/files` (count invariance
in page size and with `speaker=` and `held=true`), `GET /api/files/metadata-filters` (no `SubPlan`
after the G8 rewrite), `GET /api/admin/holds` (count invariance in holds), speaker occurrences and
cross-media (count invariance), `GET /api/tasks` (count invariance in files and tasks, plus a test
that paging returns the same items as before), `GET /api/tasks/system/health` (count invariance),
voiceprint purge statements (no `SubPlan`, extending the existing test), `list_unused_tag_rows`
(anti-join), and one usability test per new index.

## 6. Lanes and sequencing

House rules that apply to every lane: one agent per lane, in its **own** git worktree on its own
branch off the integration branch (`.claude/worktrees/1337-<lane>` or a sibling
`/mnt/nvm/repos/transcribe-app-wt-v070-1337-<lane>`), one commit per concern with a conventional
message, hooks on (never `--no-verify`), never push, never merge, never touch another checkout.
Lanes run only targeted tests plus static checks on touched files; no full suites and no
`--all-files` pre-commit in a lane. The coordinator merges finished lanes `--no-ff` into the
integration branch and runs exactly one full sweep per wave. Host budget: at most 3 lanes at once.

### 6.1 Per-lane verification recipe

For each change:

1. **Red first** against the old code without touching the shared checkout:
   ```bash
   RED="$(mktemp -d -t ot-redcheck.XXXXXX)" && git archive HEAD | tar -x -C "$RED"
   cp backend/tests/<path>/test_<new>.py "$RED/backend/tests/<path>/"
   cp backend/tests/query_budget.py "$RED/backend/tests/"        # if the lane needs the helper
   cd "$RED/backend" && /mnt/nvm/repos/transcribe-app/backend/venv/bin/pytest -n 1 -p no:cacheprovider tests/<path>/test_<new>.py -v
   ```
   Expect the new assertions to fail for the stated reason (the count grows, a `SubPlan` is
   present, the index name is absent). Paste the failing assertion line in the commit body.
2. Green on the lane branch: same command from the lane worktree's `backend/`.
3. Check the run used the new code: the new test names appear in the output.
4. Static: `backend/venv/bin/ruff check <files>`, `backend/venv/bin/ruff format --check <files>`,
   `backend/venv/bin/mypy <files>`, `cd backend && python3 ../scripts/audit-tests.py tests` (0 open
   findings), and for L5 `python3 scripts/guards/check_migration_safety.py`.
5. Database for targeted tests: tests run against the Postgres they are pointed at
   (`POSTGRES_HOST=localhost:5176` by default). Lanes L1-L4 change no schema and may use the live
   dev stack's database (tests are savepoint-isolated). L5 changes the schema and must point at its
   own `--fresh` stack by exporting its `*_PORT` variables.
6. Measurement for the lane's hypotheses (section 4.4) on the shared `perf1337` stack, one lane at a
   time (announce the window; a measurement under another lane's load is void).
7. Commit with an explicit pathspec: `git add <files> && git commit -- <files>`.

### 6.2 Waves

**Wave 0: tooling and baseline (sequential, starts now)**

- **L0 tooling** (branch `v070/1337-l0-tooling`). Commits:
  1. `feat(scripts): postgres-only synthetic dataset for query review` (`backend/app/scripts/scale_dataset/{__init__,__main__,pg_synthetic}.py`, `scripts/seed-query-review.sh`). Test: a unit test seeds a tiny shape (`--files 20 --speakers-per-file 3`) into the test DB within `db_session` and asserts row counts, determinism (two runs produce identical hashes of the generated rows) and that the live-stack guard refuses port 5176.
  2. `test(db): statement-count helper for N+1 regression checks` (`backend/tests/query_budget.py`, self-test).
  3. `feat(scripts): query-review capture and explain runner` (`scripts/perf/query-review/{drive.py,explain.sh,queries/*.sql,README.md}` where the README is the run checklist from section 4, not prose about findings).
- **M1 baseline run** (coordinator or L0 agent, after L0 merges): sections 4.1 to 4.3 on
  `perf1337`, including the #1328 index-absent/present pair. Output: `summary.md` with a verdict per
  section 3 id. Append the verdict table to section 9 of this file and commit
  `docs(design): record the #1337 baseline measurement`.
- **Gate into wave 1**: only `confirmed` ids get work in wave 1 and 2. `refuted` and `below budget`
  ids are recorded and closed.

**Wave 1: rewrites and N+1 fixes (parallel, 3 lanes), after M1**

- **L1 gallery, holds, tags, search** (`v070/1337-l1-gallery`). Owns
  `api/endpoints/files/{listing,filtering,trash,bulk_action_handlers}.py`,
  `services/{content_hold_records,tag_collisions,permission_service}.py`,
  `services/collection_visibility.py`, `services/search/access_filter.py`. Candidate commits (each
  only if confirmed): G10 preload, G3 `id` tie-breaker, G7/G8 held rewrite, G5 tenant-bound tag
  filter, G13 `NOT IN` -> `NOT EXISTS`, T4 `selectinload(collection)`, S1 collection-rank rewrite,
  T2 deferral. `hold_guard.held_pred` is shared by write paths: add a new filter form beside it, do
  not change its semantics.
- **L2 speakers** (`v070/1337-l2-speakers`). Owns `api/endpoints/speakers_list*.py`,
  `speakers_occurrences.py`, `speaker_profiles.py`, `services/speaker_matching/profiles.py`,
  `services/clustering/{queries,verification}.py`, `services/opensearch_service/speaker_metadata.py`,
  `services/speaker_status_service.py`. Candidates: P1/P2 `NOT IN`, P6/P7 `contains_eager` +
  `preload_files`, P9 combined counts, P10 chunked startup sync, dead `compute_status_for_speakers`.
- **L3 tasks and admin** (`v070/1337-l3-tasks-admin`). Owns `api/endpoints/{tasks,tasks_system_health,tasks_recovery,admin,system}.py`,
  `utils/stats_helpers.py`, `services/analytics_service.py`. Candidates: K1 SQL paging (largest
  item; keep `TaskFilteringService` semantics, add a parity test that compares old and new results
  on a seeded set before deleting the Python path), K2/K3 eager loads, A1 `async def` -> `def`,
  A2 combined aggregates.
- Coordinator: merge L1-L3 `--no-ff`, run the wave sweep (6.3), tag `query-review-w1`.

**Wave 2: privacy/purge and indexes (parallel, 2 lanes), after wave 1 merges**

- **L4 privacy and purge** (`v070/1337-l4-privacy`). Owns `services/privacy/*`,
  `services/file_cleanup_service.py`, `services/gdpr_erasure_*.py`, `tasks/{speaker_embedding_consistency,voiceprint_retention,cleanup}.py`,
  `services/search/index_retry.py`, `services/speaker_identity_history.py`. These are
  security-critical modules: extend the existing purge tests, never loosen one, and run
  `./scripts/run-mutation-tests.sh --module <module>` for any module in its scope that the lane
  changes (opt-in, one module at a time). Candidates: V1/V2 split OR, V3/V6 combined counts, V7
  batched citation scrub, V9/V10 batching, V11 lateral rewrite, V14 N+1 and convergence check.
- **L5 measured indexes** (`v070/1337-l5-indexes`). Owns `backend/alembic/versions/v<NNN>_query_review_indexes.py`,
  `backend/app/db/perf_indexes.py`, `migrations.py` registration, model `__table_args__`,
  `schema_detect/` probe and arm, the ladder test, the consistency test, the lane-table row in
  `backend/alembic/CLAUDE.md`. Only indexes M1 (or the wave 1 after-measurement) proved under 5.1.
  Candidate list to test, not to ship blind: T1 trash list, K4 `task(created_at)`, P4
  `suggestion_source` partial, P8 inbox partial, A2/V13 `media_file(completed_at)`, V16
  `chat_conversation(last_message_at)`.
- **M2 after run**: restore the same seed dump, re-run section 4.3 on the integration branch, append
  the before/after table to section 9.
- Coordinator: merge, sweep, tag `query-review-w2`, then the branch goes to a PR into `master`
  (never a local merge onto `master`).

### 6.3 The one full sweep per wave (coordinator, on the integration branch)

```bash
./opentr.sh start dev                                    # stack up, migrations applied
./scripts/run-integration-tests.sh                       # the pre-merge gate
cd frontend && npm run check && cd ..                    # only if a lane touched frontend (none planned)
scripts/safe-precommit.sh run --all-files
scripts/safe-precommit.sh run --all-files --hook-stage pre-push
```

Wave 2 additionally: `./opentr.sh reset dev` is **not** run against live data; instead the L5
revision is tested both ways on a fresh stack (`./opentr.sh start dev --fresh l5mig` from empty,
then stop, start again over the existing volume) and the runner's log shows the indexes `built`
then `exists`.

### 6.4 Out of scope for v0.7.0 (later; file a GitHub issue only for the major ones)

- Trigram (`pg_trgm`) indexes for G4 and the P2 label search: needs `CREATE EXTENSION` in a
  revision and an `IndexSpec` extension for GIN; v0.8.0 issue, with the measurement from M1 attached.
- Keyset pagination for the gallery and inbox (API change).
- FK `ON DELETE CASCADE` on `transcript_segment` and friends + `passive_deletes=True` (V8): a
  constraint migration on the largest table (R5 `NOT VALID` + `VALIDATE` shape); v0.8.0 issue.
- `raiseload("*")` on production list queries.
- `pg_stat_statements` on by default in the shipped compose (scale plan open question 3).
- OpenSearch-side deep paging for audit logs; S3's 65k uuid terms list.
- Paging `GET /speakers` full list (P1) and the profiles list (P5) if the frontend does not page
  today (API + UI change).
- Approximate counts (`pg_class.reltuples`) for `/admin/stats`: semantics change, owner decision.

## 7. Risks and open questions (each with the default to take)

| # | Risk / question | Default |
|---|---|---|
| R1 | The seed is synthetic; plans can differ from a real library (correlation between columns, text lengths). | Use the distributions in 4.2; where a verdict is borderline, re-check on the owner's real dataset only with his go-ahead, never by touching live data. |
| R2 | `ALTER SYSTEM` settings are lost if the fresh stack is destroyed, and a `-c` flag added to compose later would silently override them. | Re-run 4.1 step 4 after any `fresh-destroy`; the `pg_settings` preflight (`source` column) catches overrides. |
| R3 | `auto_explain.log_analyze` slows every statement, inflating absolute numbers. | Compare only within the same settings; take absolute timings from `EXPLAIN (ANALYZE)` runs, not from the driver's wall time. |
| R4 | Seeded rows trigger periodic jobs (LLM summarisation, ASR). | Keep the injector's inertness rules (`completed_at` NULL, terminal statuses); `completed_at`-dependent statements are measured inside rolled-back transactions. |
| R5 | A lane edits `.py` under `backend/app/` while a measurement runs on a stack bind-mounted to that checkout (hot reload dispatches `search_index_maintenance`). | Measure only from a quiet worktree; the perf stack is started from the worktree being measured and nobody edits it during the window. |
| R6 | The count helper misses statements from a thread or engine it did not listen on. | Listen on the test connection and the app engine; the helper self-test includes a `SessionLocal()` path. If a third engine appears, fail the self-test rather than count silently. |
| R7 | `scripts/test-in-worktree.sh` was removed on this branch (CHANGELOG: `./scripts/run-backend-tests.sh --throwaway-db` replaces it); master's root `CLAUDE.md` still names it, and this branch's already does not. | Use `./scripts/run-backend-tests.sh --throwaway-db` for DB-backed tests without the stack, and a `--fresh` stack for L5. Nothing to fix. |
| R8 | Revision number collision with another v0.7.0 lane. | Derive at implementation time, reserve in the lane table in the same commit, coordinator re-chains at merge. |
| R9 | `held_pred` / `not_held_pred` are shared by destroy guards; a rewrite could weaken a legal-hold guarantee. | Do not change them. Add a separate filter-only form and test it returns the same id set as `held_file_ids()` on a seeded mix. |
| R10 | K1 (`GET /tasks`) rewrite changes filtering semantics silently. | Parity test against the old Python path on a seeded set first, delete the old path in the same lane once green. |
| R11 | An N+1 fix in privacy code (G10, P6) changes what is hidden. | Assert the response body is identical before and after on a fixture with voiceprints off for one scope, in the same test as the count. |
| R12 | Measurements disagree with a hypothesis in this file. | The measurement wins. Mark the row `refuted` in section 9 and do no work on it. |
| R13 | v0.7.0 runs out of time. | Priority order: L0 + M1 (closes #1328 with numbers), then K1, G10, P6/P7, T4 (user-visible latency and N+1), then V1/V2/V7, then indexes. Anything unfinished moves to a v0.8.0 issue with its M1 numbers. |
| R14 | `/tmp` is cleaned on this host. | All run artifacts under `~/ot-cleanup-work/1337/perf/`. |
| R15 | V14 non-convergence (voiceprint retention re-selecting purged files) is a correctness bug, not a performance one. | Verify in L4 with a test; if confirmed, fix in L4 and add a CHANGELOG line. |

## 8. Definition of done for #1337

- M1 and M2 run directories exist with `summary.md`; section 9 holds the before/after table with
  controls; #1328's before/after numbers are recorded.
- Every `confirmed` id in section 3 is either fixed with a regression check that was seen red, or
  moved to a named v0.8.0 issue with its numbers.
- Every new index passed 5.1, is built by the runner, has a detection arm and a consistency test,
  and `check_migration_safety.py` is clean without a baseline entry.
- Both wave sweeps green; CHANGELOG `Unreleased` lists the user-visible speedups and the
  correctness fixes (G3 order stability, A1 event-loop blocking, any V14 fix).

## 9. Results (appended by the implementer)

_Empty until M1._
