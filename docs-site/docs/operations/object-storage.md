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
issued), and server-side encryption at rest is your provider's responsibility. Read
[Production deployment](./production-deployment.md) and the
[environment variable reference](../configuration/environment-variables.md#object-storage) before
switching. Switching an existing install means copying its objects into the new store yourself.

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
