# Storage hardening follow-ups: remove the shared-object-key class (issue #1357, storage items)

Status: PLAN ONLY. Written 2026-11-01 against the `release-v061` worktree. Scope: the four storage
items of #1357 (re-import race, concurrent purges, avatar uploads, other shared-key shapes, which
includes the `thumbnail_migration` shared jpg item). The other #1357 items (cache 500/503, audit
`pinned_by`, audit type-clash scan) are out of scope here. The "orphan sweep deletions are
irreversible on versioned buckets" docs note is folded in at step 7 since it touches the same doc.

Read before starting: root `CLAUDE.md` (pre-commit rules, red-first via `git archive`, one
writer commits), `backend/app/services/CLAUDE.md`, `backend/tests/CLAUDE.md`,
`backend/alembic/CLAUDE.md`, `backend/app/db/CLAUDE.md`.

## 1. Background: why patches are the wrong shape

Since object deletes became "remove every version" (`storage_versions.remove_completely`,
`minio_service.delete_file`), deleting a key that another row still points at destroys that
row's object and its history. Today correctness depends on
`file_cleanup_service._drop_shared_paths` (called from `_load_purge_plan`, phase 1 of
`purge_media_file`): it drops from the plan any key that another `media_file` row references.
The check runs in phase 1. The delete runs in phase 2 (`_purge_external_copies`) with no
transaction and no lock, which is a check-then-act gap. That gap is the cause of items 1 and 2.

Locking that gap means holding a lock (advisory or row) across phase 2's object-store and
OpenSearch round trips, which conflicts with the three-phase "no transaction during external I/O"
rule documented on `purge_media_file`. Reference counting needs a new table and every writer
updating it correctly. **Chosen design: make every object key unique to one row by construction,
and enforce it with partial unique indexes.** If no key can be shared, a delete can't remove
another row's object, and `_drop_shared_paths` is deleted rather than hardened.

## 2. Enumeration of every object-key derivation (item 4)

Found with `rg -n 'upload_file\(|put_object\(|object_name *=|_path *= *f"' backend/app`.

| # | Where | Key shape | Per row? |
|---|---|---|---|
| K1 | `utils/filename.py:118` `get_safe_storage_filename` (upload, prepare_upload, multipart, watch sources) | `user_{uid}/file_{fid}/{name}` | yes |
| K2 | `media_download_service.py:1673` URL import original | `media/{uid}/{uuid4}{ext}` | yes |
| K3 | `playback_rendition.rendition_object_name` | `{storage_path}{RENDITION_SUFFIX}` | yes, iff storage_path is |
| K4 | `tasks/thumbnail.py:79` | `user_{uid}/file_{fid}/thumbnail.webp` | yes |
| K5 | `utils/thumbnail.py:221` | `user_{uid}/file_{fid}/thumbnail_{stem}.{ext}` | yes |
| K6 | `tasks/thumbnail_migration.py:202` | old jpg path with `.webp` | inherits the old path's sharing |
| K7 | `video_processing_service.py:459,883` derived cache | `derived/{fid}_...` (cache bucket) | yes |
| **S1** | `media_download_service.py:1454` `_download_media_thumbnail_sync` | `user_{uid}/{extractor}_{video_id}/thumbnail.jpg` | **NO**: same user + same video means one key for two rows |
| **S2** | `api/endpoints/speaker_profiles.py:938` avatar upload | `avatars/{uid}/{profile_uuid}.{ext}` | per profile, but **reused across uploads**, so two in-flight uploads share it |
| **S3** | `storage_recovery_service.py:220` `register_object` (and `scripts/reingest_minio.py:141`) | existing key, as found | **NO under concurrency**: `existing_storage_paths(db)` is a snapshot read before the loop (`:275`), so two concurrent reingest runs (or a run racing a normal ingest's commit) can register the same key twice |
| K8 | `erasure_ledger_service.py:158`, `media_revocation_probe.py:97`, `backup_service`, `asr/*_provider` | journal / probe / backup / vendor-job keys | not row-referenced; out of scope |
| K9 | `scripts/corpus_injection/rows.py:52` | `user_{uid}/file_{fid}/no-media...` | yes (script only) |

S1, S2, S3 are the shared shapes. K6 only shares when its input does (S1 rows are excluded by
`source_url IS NULL`, but S3 rows are direct-upload-shaped and are not excluded).

## 3. Failure scenarios and red tests

Every test must be seen failing against HEAD using a `git archive HEAD` tree
(`RED="$(mktemp -d -t ot-redcheck.XXXXXX)"`, per root CLAUDE.md), then passing with the fix.
Tests need the live dev stack (`./opentr.sh start dev`); MinIO-backed tests auto-enable.
Run `cd backend && python3 ../scripts/audit-tests.py tests` with 0 open findings and **no new
allowlist entries** (an acceptance criterion of #1357).

### Item 1: re-import race (S1)
Scenario: a user has row A with thumbnail `user_1/youtube_X/thumbnail.jpg`, no other reference.
`purge_media_file(A)` phase 1 finds no other reference and keeps the key in the plan. Before
phase 2 runs, the user re-imports video X, which creates row B and uploads a fresh thumbnail to
**the same key**. Phase 2 deletes every version, so B shows no thumbnail. If B's upload lands
mid-delete, `remove_completely` raises `StorageDeleteIncompleteError` and the purge reports a
partial residual.

Red test `backend/tests/integration/test_url_import_thumbnail_key_is_per_row.py`:
1. `test_reimport_between_purge_phases_keeps_new_thumbnail`: create A with a real MinIO object at
   the S1 key; call `_load_purge_plan(db, A)`; `db.commit()`; create row B and obtain its key via
   `MediaDownloadService._download_media_thumbnail_sync(media_info, user_id, media_file_id=B.id)`
   (stub `requests.get` to return fixed bytes; the `media_file_id` kwarg is new, so in the RED
   tree, call it the way HEAD does and assert keys differ). Run `_purge_external_copies(plan)`;
   assert B's thumbnail object still exists (`stat_object`). RED on HEAD: same key, object gone.
2. `test_two_imports_of_one_video_get_distinct_thumbnail_keys`: pure unit, no MinIO. Two calls
   with different `media_file_id` return different keys, both matching `^user_\d+/file_\d+/`.

### Item 2: concurrent purges of two rows sharing a key (S1, S3)
Scenario: rows A and B share key K. Two purges run at once. Each phase 1 sees the other row,
drops K, both rows delete, and K is never deleted. `storage_orphan_sweep_task._FILE_KEY_RE`
(`^user_(\d+)/file_(\d+)/`) never lists `user_<uid>/<source>_<id>/` keys, so K leaks forever.

Red test, same file: `test_concurrent_purges_of_rows_sharing_a_key_leave_no_object`: create A and
B referencing K (legacy shape) plus the object; build both plans (`_load_purge_plan` on two
sessions) **before** either phase 3; then run phase 2 and phase 3 for both; assert K does not
exist. RED on HEAD (both plans dropped K). With the fix, the shared state can't be constructed:
the test becomes `test_two_rows_cannot_reference_one_thumbnail_key`, asserting
`IntegrityError` on the second insert, plus the migration test in 4.3 covers legacy data.
Write both: the first proves the bug on HEAD, the second the structural fix. After the fix,
delete the first one rather than keep a test that can no longer construct its precondition, and
record in the PR body that it was seen red.

### Item 3: concurrent avatar uploads (S2)
Scenario in `upload_profile_avatar` (`speaker_profiles.py` ~920-950): requests R1 and R2 both
upload `.jpg` to profile P whose current `avatar_path` is `avatars/1/P.jpg`. R1 deletes old
(`delete_file`, all versions), uploads to `avatars/1/P.jpg`, commits. R2 then runs its "delete old"
on the same key, which removes **R1's new object** and every version, and uploads its own. With
R2's upload landing before R1's delete instead, the profile ends up pointing at a key with no
object. Different-extension uploads leak the loser's object (`P.png` is never referenced again).

Red test `backend/tests/integration/test_avatar_upload_concurrency.py`:
- `test_replacing_an_avatar_never_deletes_the_key_the_profile_now_points_at`: deterministic
  interleave. Monkeypatch `minio_service.delete_file` to run a second upload (call the endpoint
  function or the extracted helper from 4.2) before delegating to the real delete; afterwards
  assert `stat_object(profile.avatar_path)` succeeds. RED on HEAD.
- `test_superseded_avatar_object_is_deleted`: two sequential uploads with different extensions
  leave exactly one object under `avatars/{uid}/{profile_uuid}`. RED on HEAD for the
  concurrent-ext variant only, so drive it with the same interleave hook.

### Item 4a: duplicate registration in reingest (S3)
Scenario: two `reingest_minio` runs (or recovery during an upload) both snapshot
`existing_storage_paths`, both call `register_object` for key K, giving two rows with
`storage_path = K` and playback key `K + suffix`. Purging one deletes the other's original.
Red test `backend/tests/integration/test_storage_path_unique.py`: insert two `MediaFile` rows with
the same non-empty `storage_path`; expect `IntegrityError`. RED on HEAD (no constraint).
Plus `test_register_object_twice_is_a_skip_not_a_crash`: calling the reingest loop twice for the
same key results in one row and `skipped_existing == 1`, which needs the fix in 4.4.

### Item 4b: `thumbnail_migration` deleting a shared jpg (K6)
Scenario: two direct-upload-shaped rows (S3) share `.../thumb.jpg`; `_migrate_single_thumbnail`
for row A uploads `.webp` and deletes the jpg, and row B now points at a deleted key.
After 4.3 the precondition is impossible (unique `thumbnail_path`). Test:
`backend/tests/unit/test_thumbnail_migration_unique_paths.py` asserts that the index named in 4.3
exists on `media_file.thumbnail_path` (query `pg_indexes`), so removing the index fails a test.
The migration test in 4.3 covers the legacy data.

## 4. Fix, in order

### 4.1 Per-row URL-import thumbnail key (S1)
`backend/app/services/media_download_service.py`:
- `_download_media_thumbnail_sync(self, media_info, user_id)` becomes
  `(self, media_info, user_id, media_file_id: int)`; key
  `f"user_{user_id}/file_{media_file_id}/thumbnail.jpg"`. Drop `video_id` / `extractor` from the
  key (keep them in the log line).
- `_get_thumbnail_with_fallback` (`:332`) already receives `media_file_id`; pass it through at
  `:371`. Check every other caller with `rg -n "_download_media_thumbnail_sync" backend/`
  (including tests and any playlist path) and update them all.
- The key now matches `_FILE_KEY_RE`, so the orphan sweep covers it. Update the module docstring
  of `tasks/storage_orphan_sweep_task.py` (lines ~10-13) so it no longer lists
  `user_<uid>/youtube_<id>/` as an out-of-scope new shape, only as legacy.

### 4.2 Avatar: unique key per upload, swap under row lock (S2)
`backend/app/api/endpoints/speaker_profiles.py` `upload_profile_avatar`:
1. Validate type and size (unchanged).
2. `object_name = f"avatars/{current_user.id}/{profile.uuid}/{uuid4().hex}.{ext}"`, then upload it
   **before** taking any lock (no lock held across I/O).
3. `SELECT ... FOR UPDATE` on the profile row
   (`db.query(SpeakerProfile).filter(SpeakerProfile.id == profile.id).with_for_update().one()`),
   then read `previous = locked.avatar_path`, set the new path, `touch_profile_interaction`,
   `db.commit()`.
4. After commit, `delete_file(previous)` if `previous` is set and differs (best effort, warn on
   failure; a leftover is a leak, not data loss). Each request deletes only the key it replaced
   under the lock, so no request can delete the key a profile currently points at.
5. On commit failure: `db.rollback()`, then `delete_file(object_name)` (our own fresh key).
Apply the same lock-then-delete-previous order in the avatar DELETE endpoint (just below,
`@router.delete("/profiles/{profile_uuid}/avatar")`). Extract steps 2-5 into a small helper in
`backend/app/services/` (for example `speaker_avatar_service.replace_avatar`) if it keeps the
endpoint readable and gives the test a seam. Old `avatars/{uid}/{uuid}.{ext}` keys stay valid,
so no data migration is needed. Check that GDPR/account purge removes avatars by **prefix**
`avatars/{uid}/` (search for `avatar_path` in `file_cleanup_service.py` and the account purge);
if it deletes by exact `avatar_path` only, that still works, since one key per profile is live.

### 4.3 Alembic migration: dedupe, then partial unique indexes
Revision number: derive the next free `vNNN` with
`python3 scripts/release-tests/lib/alembic-head.py` **at write time**. Head was `v451` when this
was written, and v452+ may already be taken by other lanes, so re-derive and don't hardcode.
Follow `backend/alembic/CLAUDE.md` for file layout and idempotency. Never autogenerate inside the
hot-reload container.

`upgrade()`:
1. **thumbnail_path duplicates** (legacy S1 and any S3): for each `thumbnail_path` held by >1 row,
   keep the row with the lowest `id`, and set `thumbnail_path = NULL` on the rest. Thumbnails can
   be regenerated (see 4.5); originals can't, which is why this is only done for thumbnails.
2. **storage_path duplicates** (S3): `SELECT storage_path, array_agg(id) ... WHERE storage_path
   NOT IN ('', 'pending') GROUP BY 1 HAVING count(*) > 1`. These can't be resolved by DB-only
   code: each row needs its own copy of the object. If any exist, `raise RuntimeError` naming
   the ids and the remediation script (4.6). This stops startup loudly instead of shipping a
   constraint that silently fails to build. Confirm the count on the live dev DB **before**
   merging (`/db-shell`); expected 0.
3. `CREATE UNIQUE INDEX IF NOT EXISTS uq_media_file_thumbnail_path ON media_file (thumbnail_path)
   WHERE thumbnail_path IS NOT NULL AND thumbnail_path <> ''`.
4. Same for `storage_path` (`WHERE storage_path NOT IN ('', 'pending')`; check
   `upload.py:150` and `watch_sources/processing.py:324` for the placeholder values in use, and
   `scripts/corpus_injection/injector.py:116`) and for `playback_path`.
`downgrade()`: drop the three indexes. The NULLed thumbnails are not restored; say so in the
docstring.
Also add the indexes to the SQLAlchemy model (`backend/app/models/media.py`, `Index(...,
unique=True, postgresql_where=...)`) so `run-integration-tests.sh`'s schema-drift check stays
clean. See `backend/app/models/CLAUDE.md`.
Migration test `backend/tests/integration/test_vNNN_unique_object_keys.py`: seed two rows sharing
a thumbnail; run the upgrade function on a scratch schema (use the existing migration-consistency
test pattern; search `rg -ln "migration_consistency" backend/tests`); assert one row keeps the
path and the other is NULL, and that a duplicate `storage_path` makes the upgrade raise.

### 4.4 Reingest handles the constraint
`storage_recovery_service.py` loop (`:276-300`): wrap `register_object` + commit in
`try/except IntegrityError`, then `db.rollback()`, `summary.skipped_existing += 1`, continue. Use
a narrow `except IntegrityError`, not a broad except (see the fail-open guidance in root
CLAUDE.md). Same treatment anywhere else `storage_path` / `thumbnail_path` is assigned from an
external key (`rg -n "thumbnail_path *=|storage_path *=" backend/app`).

### 4.5 Regenerate thumbnails nulled by the migration
Thumbnails for completed rows with `thumbnail_path IS NULL` and a non-empty `storage_path`:
check whether something already backfills them (`rg -n "thumbnail_path.is_\(None\)" backend/app`).
If not, extend the startup dispatch that already runs `migrate_thumbnails_to_webp` (find it with
`rg -n "migrate_thumbnails_to_webp" backend/app`) with a batched task that calls
`generate_thumbnail_task.delay(file_id, user_id, storage_path)` (`tasks/thumbnail.py:23`), using
the same read-batch / no-session-during-I/O shape as `thumbnail_migration.py`. It writes the
per-row K4 key.

### 4.6 Remediation script for storage_path duplicates (only if 4.3 step 2 can fire)
`backend/app/scripts/split_shared_storage_paths.py`: for each duplicate group, keep the lowest id;
for each other row, server-side copy the object to
`get_safe_storage_filename(name, uid, fid)`, update `storage_path`, NULL `playback_path` (it
regenerates), and commit per row. Dry-run by default. Mention it in the RuntimeError message.

### 4.7 Delete the old mechanism
With the indexes in place, `_drop_shared_paths` can only ever return `[]`. Delete it, the
`_PATH_KEYS` constant if unused elsewhere, the `plan["shared_paths"]` key and its readers
(`rg -n "shared_paths" backend/`), and update `backend/tests/api/test_purge_shared_object_keys.py`
(delete it, or rewrite it as the `IntegrityError` assertions). Update the `purge_media_file`
docstring. Per root CLAUDE.md, don't leave two paths doing one job.

## 5. Sequencing
1. Red tests for items 1, 2, 3, 4a, all seen failing in an `ot-redcheck.` archive tree. Commit
   them together with their fixes, not as failing commits on the branch.
2. 4.1 (thumbnail key), 4.4 (reingest), 4.2 (avatar). These are independent, so batch the
   `backend/app` edits (each edit hot-reloads the dev backend).
3. 4.3 migration + model indexes + migration test; then 4.5; then 4.6 only if needed.
4. 4.7 deletion.
5. Docs + CHANGELOG (section 7).
6. Gates: `./scripts/run-integration-tests.sh` (includes schema drift), then
   `scripts/safe-precommit.sh run --all-files` and `... --hook-stage pre-push`. One writer commits.
Suggested commits: `fix(storage): key URL-import thumbnails per media file`,
`fix(speakers): give each avatar upload its own key and swap under a row lock`,
`fix(db): enforce one media row per object key`, `refactor(purge): remove shared-key reference check`.

## 6. Risks and rollback
- **Top risk: the migration on installs with duplicate `storage_path` rows.** Step 2 refuses to
  start the backend. That's intentional (a silent constraint gap is worse), but the error has to
  name the remediation script, and the release notes have to say so. Mitigation: measure the
  count on dev and in `test-upgrade.sh` rehearsal data before release.
- Index build locks `media_file` for writes during the build. Fine at current table sizes; if a
  large install is a concern, use `CREATE UNIQUE INDEX CONCURRENTLY` in an autocommit block (see
  how other revisions do it, if any).
- Rows whose thumbnail was NULLed show a placeholder until 4.5 regenerates them.
- Avatar: a crash between upload and commit leaves one unreferenced `avatars/.../<hex>.<ext>`
  object. That's a bounded leak, not loss. Optionally extend the orphan sweep to `avatars/` later
  (not in this plan).
- Rollback: revert the commits; `downgrade()` drops the indexes. Per-row thumbnail keys written
  meanwhile remain valid paths. NULLed legacy thumbnails stay NULL (regenerated ones persist).

## 7. Docs and CHANGELOG
- `backend/app/services/CLAUDE.md`: add a short "object keys are one per row, enforced by
  `uq_media_file_*`" note, and list the shapes from section 2.
- `docs-site/docs/`: find the storage/orphan-sweep page (`rg -ln "orphan sweep" docs-site/docs`)
  and add: (a) new URL-import thumbnails are now covered by the sweep; (b) the #1357 note that
  sweep deletions remove every object version on a versioned bucket and are irreversible.
- Release notes / upgrade notes: the migration may refuse to start when it finds duplicate
  `storage_path` rows, plus the remediation command.
- `CHANGELOG.md` `[Unreleased]`:
  - `### Fixed`: purging a file can no longer delete another import's thumbnail or leak it;
    concurrent avatar uploads no longer delete each other's image; duplicate reingest
    registrations are skipped.
  - `### Changed`: URL-import thumbnails are stored per media file; avatars are stored under a
    per-upload key; `media_file` object-key columns are unique (migration `vNNN`).
- i18n: no user-facing strings change, so no locale edits.
