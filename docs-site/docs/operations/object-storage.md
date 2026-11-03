---
sidebar_position: 5
title: Object storage (S3)
description: What OpenTranscribe stores in its S3 object store, why the bundled store changed from MinIO to Silo in 0.6.0, and how to check or replace it.
---

# Object storage (S3): what we use and why

OpenTranscribe keeps uploaded media in an S3-compatible object store. The bundled store runs as
the `minio` service in `docker-compose.yml`. From 0.6.0 that service runs **Silo**, a maintained
fork of MinIO, instead of MinIO itself. The service name, settings, ports and data volume are all
unchanged.

## What OpenTranscribe stores in it

- Your uploaded audio and video files, and the browser-playable renditions made from them.
- Thumbnails and other derived media.
- Generated exports (bulk exports expire on a lifecycle rule).
- A cache bucket used by the processing pipeline.

Transcripts, speakers, users and settings live in PostgreSQL, and search data lives in OpenSearch.
Only media and exports are in the object store. Browsers upload and play media through presigned
URLs, so large files do not stream through the API.

## What changed in 0.6.0: MinIO to Silo

The `minio` service now runs `docker.io/pgsty/silo:RELEASE.2026-10-26T00-00-00Z`, pinned by
digest. Silo is the AGPL-3.0 community fork of MinIO maintained by PGSTY. The image is multi-arch
(amd64 and arm64) and pulls anonymously.

Why we had to change:

- **MinIO removed its Docker Hub images in 2025.** OpenTranscribe 0.5.x had moved its pin to
  `quay.io`, but as of 2026-10-31 that registry also refuses anonymous pulls, so a fresh install
  could not fetch the old image.
- **The community MinIO project is no longer maintained.** The `minio/minio` repository is
  archived and points users to the commercial AIStor product.
- **The last community release line has published HIGH-severity advisories with no community fix.**
  The fixes exist only in the commercial line and in forks. Mirroring the old image would have
  restored installs while shipping that known state. See
  [MinIO's security advisories](https://github.com/minio/minio/security/advisories).
- **Silo keeps MinIO's interface.** It keeps the `MINIO_*` environment variables, the `/minio/*`
  routes (including the admin API we use for revoking media URLs) and MinIO's on-disk format. The
  project says an existing MinIO data directory can be used in place with no data copy.

Our existing data volume is read by Silo unchanged, including objects written with
`MINIO_KMS_AUTO_ENCRYPTION=on`.

The reasoning, the alternatives and what other projects chose are in
[Object store decision](../developer-guide/object-store-decision.md).

## What did not change

- The compose service is still named `minio`, and the container is still `opentranscribe-minio`.
  Presigned URLs and the proxies depend on the `minio:9000` host name.
- All settings: `MINIO_ROOT_USER`, `MINIO_ROOT_PASSWORD`, `MINIO_KMS_SECRET_KEY`,
  `MINIO_KMS_AUTO_ENCRYPTION`, `MINIO_PORT`, `MINIO_CONSOLE_PORT` and the rest. Your `.env` needs
  no edits.
- Ports (API on `MINIO_PORT`, console on `MINIO_CONSOLE_PORT`, loopback only) and the start
  command.
- The data volume (`minio_data`, or your NAS path if you use the NAS overlay) and the bucket
  names.
- How the application behaves: uploads, playback, downloads, multipart uploads and the
  immediate revocation of media URLs when a file is quarantined.

## Check what you are running

```bash
# The image the service is configured to use
rg -n "image:.*silo" docker-compose.yml

# The image of the running container
docker inspect opentranscribe-minio --format '{{.Config.Image}}'

# The server version
docker compose exec minio silo --version
```

Use `./opentranscribe.sh compose-files` (installed deployments) or `./opentr.sh` (a clone) if you
need the full list of compose files for the `docker compose` command above. A deployment that
still shows `quay.io/minio/minio` has not picked up the 0.6.0 compose file.

## Using your own S3

You do not have to use the bundled store. Setting `STORAGE_BACKEND=s3` points OpenTranscribe at
AWS S3, or any S3-compatible provider through `S3_ENDPOINT_URL`:

```bash
STORAGE_BACKEND=s3
S3_REGION=us-east-1
S3_USE_IAM_ROLE=true      # AWS credential chain; set false and supply AWS_ACCESS_KEY_ID / AWS_SECRET_ACCESS_KEY otherwise
# S3_ENDPOINT_URL=        # only for a non-AWS S3-compatible provider
# S3_CONFIGURE_BUCKET_CORS=false
# S3_CORS_ALLOWED_ORIGINS=
```

The default is `STORAGE_BACKEND=minio`. Two things differ on a non-bundled store: an
already-issued media URL for a quarantined file stays valid until it expires (new URLs are never
issued) unless you attach the quarantine bucket policy and grant the app `s3:GetObjectTagging`,
`s3:PutObjectTagging` and `s3:GetBucketPolicy` on the media bucket
([permissions and a verification recipe](./production-deployment.md#permissions-quarantine-needs-on-s3)),
and server-side encryption at rest is your provider's responsibility. Read
[Production deployment](./production-deployment.md) and the
[environment variable reference](../configuration/environment-variables.md#object-storage) before
switching. Switching an existing install means copying its objects into the new store yourself.

### Versioned buckets and deletion permissions

If your bucket has versioning on (object lock, which a legal hold uses, requires it), deleting a
recording has to remove every version of it, not only the current one: a plain delete leaves the
old versions retrievable. OpenTranscribe does this for every delete, erasure, cache purge and
orphan sweep, and reports a delete as failed (naming what is left) when a version survives, for
example one under a legal hold or a retention period. It never sends a governance-bypass header.

Grant the storage identity these actions on the media and cache buckets, in addition to the
`s3:ListBucket`, `s3:GetObject`, `s3:PutObject` and `s3:DeleteObject` it already needs:

| Action | Why |
|---|---|
| `s3:ListBucket` | to find the keys a delete selects and to confirm they are gone |
| `s3:DeleteObject` | to delete the current object |
| `s3:GetBucketVersioning` | to know whether old versions can exist (a versioned answer is cached for five minutes; "off" is never cached) |
| `s3:ListBucketVersions` | to find every version and delete marker of a key |
| `s3:DeleteObjectVersion` | to remove each version |

Without `s3:GetBucketVersioning` and `s3:ListBucketVersions` the delete still runs, but it is
**unverified**: whether old versions remain cannot be known, the server log carries an ERROR, and a
file purge or GDPR erasure records it as partial (`storage_unverified`), never complete. On a bucket
known to be versioned, a refused version listing is reported as a failed delete, because versions
certainly remain. Deleting every version is irreversible, and that includes the orphan sweep.

### One owning row per object key

Every stored object key belongs to exactly one media file, and the database enforces it with
partial unique indexes on `media_file.storage_path`, `thumbnail_path` and `playback_path`
(placeholders and empty values are ignored). Thumbnails of imported URLs are stored per media file
(`user_<id>/file_<id>/thumbnail.jpg`), so they are covered by the orphan sweep like every other
per-file object. Each avatar upload has its own key.

On upgrade the migration checks for duplicates first and never blocks startup. Legacy thumbnails
that two files shared stay with one file and are regenerated for the others in the background. If
two files share an original (`storage_path`), that index is skipped, an error is logged, and
`GET /api/admin/data-integrity/storage/keys` reports the counts. Give each extra file its own copy
with the dry-run-by-default script, then restart (or let the script build the index):

```bash
./opentr.sh shell backend
python -m app.scripts.split_shared_storage_paths            # counts only
python -m app.scripts.split_shared_storage_paths --apply    # copy, repoint, build the index
```

### Complete IAM policy reference

This section lists every S3 action the backend calls, derived from the code. A unit test
(`backend/tests/unit/test_s3_iam_docs_cover_code.py`) scans the backend for object-store client
calls and fails when this table or the example policy omits one, so it stays current. The
sections above describe the quarantine and version-delete subsets; this is the full list.

The application uses up to four separate buckets, each with its own identity if you prefer:
the media bucket and cache bucket (the storage identity), an optional backup bucket, an optional
mirror destination, and, only if you enable AWS Transcribe, its temporary bucket. A watch source
pointing at an S3 bucket needs only read access to that bucket (plus `s3:PutObject` if you enable
stitched-file write-back).

| Action | Resource | Used for |
|---|---|---|
| `s3:ListBucket` | bucket | listing keys, bucket existence and reachability probes (`HeadBucket`) |
| `s3:GetObject` | `bucket/*` | reads, downloads, stat/head, presigned download URLs |
| `s3:PutObject` | `bucket/*` | uploads (single and multipart), presigned upload URLs, the startup probe object |
| `s3:AbortMultipartUpload` | `bucket/*` | cleaning up a failed or abandoned multipart upload |
| `s3:DeleteObject` | `bucket/*` | deleting current objects (file delete, cache purge, orphan sweep, backup retention) |
| `s3:ListBucketVersions` | bucket | finding every version and delete marker of a key (versioned buckets) |
| `s3:DeleteObjectVersion` | `bucket/*` | removing each version (versioned buckets); irreversible |
| `s3:GetBucketVersioning` | bucket | knowing whether old versions can exist |
| `s3:GetObjectTagging`, `s3:PutObjectTagging` | `bucket/*` | the quarantine tag and the presigned-URL revocation probe |
| `s3:GetBucketPolicy` | bucket | the quarantine posture check and presign identity check |
| `s3:DeleteBucketPolicy` | bucket | startup removes a public-read bucket policy if one is found |
| `s3:GetLifecycleConfiguration`, `s3:PutLifecycleConfiguration` | bucket | the export-expiry rule and the abort-incomplete-multipart rule (best-effort: a refusal is logged, uploads continue) |
| `s3:PutBucketCORS` | bucket | browser-upload CORS, only when `S3_CONFIGURE_BUCKET_CORS=true` |
| `s3:CreateBucket` | bucket | creating a missing bucket at startup, and AWS Transcribe's temporary bucket |
| `s3:PutObjectLegalHold` | `bucket/*` | optional best-effort legal hold, Object Lock buckets only |

Example policy for the storage identity on the media and cache buckets. Replace the bucket names;
drop statements for features you do not use (for example the versioning and legal-hold actions on
an unversioned bucket, `s3:PutBucketCORS` and `s3:CreateBucket` if you create and configure the
buckets yourself):

```json
{
  "Version": "2012-10-17",
  "Statement": [
    {
      "Sid": "BucketLevel",
      "Effect": "Allow",
      "Action": [
        "s3:ListBucket",
        "s3:ListBucketVersions",
        "s3:GetBucketVersioning",
        "s3:GetBucketPolicy",
        "s3:DeleteBucketPolicy",
        "s3:GetLifecycleConfiguration",
        "s3:PutLifecycleConfiguration",
        "s3:PutBucketCORS",
        "s3:CreateBucket"
      ],
      "Resource": [
        "arn:aws:s3:::<your-media-bucket>",
        "arn:aws:s3:::<your-cache-bucket>"
      ]
    },
    {
      "Sid": "ObjectLevel",
      "Effect": "Allow",
      "Action": [
        "s3:GetObject",
        "s3:PutObject",
        "s3:AbortMultipartUpload",
        "s3:DeleteObject",
        "s3:DeleteObjectVersion",
        "s3:GetObjectTagging",
        "s3:PutObjectTagging",
        "s3:PutObjectLegalHold"
      ],
      "Resource": [
        "arn:aws:s3:::<your-media-bucket>/*",
        "arn:aws:s3:::<your-cache-bucket>/*"
      ]
    }
  ]
}
```

Minimal variants:

- **Plain storage, unversioned bucket, buckets pre-created:** `s3:ListBucket` on the bucket and
  `s3:GetObject`, `s3:PutObject`, `s3:AbortMultipartUpload`, `s3:DeleteObject` on `bucket/*`.
  Lifecycle rules and CORS are then your responsibility.
- **Add versioned-bucket deletes:** `s3:GetBucketVersioning`, `s3:ListBucketVersions` on the
  bucket and `s3:DeleteObjectVersion` on `bucket/*`.
- **Add quarantine enforcement:** `s3:GetObjectTagging`, `s3:PutObjectTagging` on `bucket/*` and
  `s3:GetBucketPolicy` on the bucket, plus the deny policy described in
  [Production deployment](./production-deployment.md#permissions-quarantine-needs-on-s3).
- **Backup bucket:** `s3:ListBucket`, `s3:GetObject`, `s3:PutObject`, `s3:AbortMultipartUpload`,
  `s3:DeleteObject` (retention pruning) on that bucket only.
- **Watch source or mirror destination:** `s3:ListBucket` and `s3:GetObject` to read a source;
  `s3:PutObject` and `s3:AbortMultipartUpload` on a mirror destination or a write-back source.
- **Splitting shared object keys (`split_shared_storage_paths --apply`):** a server-side copy,
  so `s3:GetObject` and `s3:PutObject` on the media bucket's `bucket/*`.
- **AWS Transcribe temporary bucket:** `s3:CreateBucket` (only if the app should create it),
  `s3:ListBucket`, `s3:PutObject`, `s3:GetObject`, `s3:DeleteObject`.

This policy has not been run through the IAM policy simulator against a live AWS bucket; validate
it in your account before relying on it.

## Roadmap

These are plans, not part of 0.6.0:

1. **Mirror the pinned Silo image into our own Docker Hub namespace**, so a third party deleting
   an image can no longer break installs.
2. **Make the backend S3-generic.** The identity that signs media URLs currently uses MinIO's
   admin API. Putting it behind an interface lets operators bring another S3 store.
3. **Evaluate SeaweedFS** (Apache-2.0, used by Sentry, PostHog and Kubeflow Pipelines) as a
   possible future default. If we ever move, the data will be copied through the S3 API into a
   new volume. We will never switch the store over your existing data volume in place.

## Licence

Silo is licensed under AGPL-3.0, the same licence as OpenTranscribe. The source is at
[github.com/pgsty/silo](https://github.com/pgsty/silo), and the tag matching the pinned release
is the Corresponding Source for the image. See the `NOTICE` file in the repository root for the
attribution of MinIO and Silo.
