# services — object storage

Moved intact from `backend/app/services/CLAUDE.md`. Read before touching `storage_backend.py`, `minio_service.py` or the storage recovery/version modules.

## Object storage: `storage_backend.py` + `minio_service.py`

`minio_service` is the API (upload/download/presign/lifecycle; the `MinIOService` class wrapper lives in `minio_bucket_admin.py` and reads `minio_service.minio_client` as a module attribute so patches of `minio_client` reach it, ~60 call sites plus the
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

- **Object keys are one per row, enforced by the database (#1357).** `uq_media_file_storage_path`,
  `_thumbnail_path` and `_playback_path` (v456, defined once in `app/db/object_key_indexes.py`)
  make a second row naming a key an `IntegrityError`, so a delete can never remove another row's
  object. That is why there is no shared-key check in the purge path any more. Key shapes: upload
  `user_<uid>/file_<fid>/<name>`; URL import original `media/<uid>/<uuid>`; URL-import thumbnail
  `user_<uid>/file_<fid>/thumbnail.jpg`; generated thumbnail `.../thumbnail.webp`; playback
  `<storage_path>.playback...`; avatar `avatars/<uid>/<profile_uuid>/<random>.<ext>` (swap under a
  profile row lock via `speaker_avatar_service`, delete only the key replaced). A NEW writer of a
  stored key must derive it from the row's own id (or a uuid), never from external identity.
