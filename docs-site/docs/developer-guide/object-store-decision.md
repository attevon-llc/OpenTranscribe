---
sidebar_position: 14
title: "Decision: the bundled object store"
description: Architecture decision record for replacing MinIO with Silo as OpenTranscribe's bundled S3 store in 0.6.0.
---

# Decision record: the bundled object store

Status: accepted by the project owner, 2026-10-31. Revisit: 2027-04-30 (six months).

Evidence lives in two research reports in the repository:
[`docs/design/s3_object_store_alternatives_v060.md`](https://github.com/attevon-llc/OpenTranscribe/blob/master/docs/design/s3_object_store_alternatives_v060.md)
(registry probes, feature inventory, options) and
[`docs/design/s3_object_store_adoption_survey.md`](https://github.com/attevon-llc/OpenTranscribe/blob/master/docs/design/s3_object_store_adoption_survey.md)
(what other projects did). For operators, see [Object storage](../operations/object-storage.md).

## Context

OpenTranscribe bundles MinIO as its S3 store (service `minio`, volume `minio_data`, `MINIO_*`
settings). By 2026-10-31:

- MinIO's Docker Hub images were gone, and `quay.io/minio/minio` refused anonymous pulls. A fresh
  install could not start.
- The `minio/minio` repository is archived and source-only.
- The last community release line carries published HIGH-severity advisories whose fixes exist
  only in the commercial AIStor line and in forks.
- Existing installs hold user media, often encrypted at rest with MinIO's KMS auto-encryption, so
  any change must not move or rewrite that data.

The S3 data plane OpenTranscribe uses (put/get, range reads, presigned GET and PUT, browser-side
multipart, prefix lifecycle, tagging) is portable. Three things are tied to the MinIO lineage: the
restricted presign identity created through MinIO's admin API, KMS-encrypted data at rest, and
small operational dependencies (`/minio/health/live`, `mc` in the container).

## Options considered

| Option | Summary | Verdict |
|---|---|---|
| **Silo** (PGSTY fork of MinIO) | Anonymous multi-arch pull, same env, routes, admin API and on-disk format. Release notes describe fixes for the advisories. | **Chosen** |
| Mirror the old MinIO image | Minutes of work, no data risk. | Rejected: ships the unpatched HIGH advisories. Emergency fallback only |
| Build old MinIO from source | Fixes one advisory only. | Rejected: worse than Silo at higher maintenance |
| Chainguard MinIO | Company-staffed security fork. Free tier has `latest` only, runs as UID 65532, no curl. | Fallback. Needs compose and data-ownership changes |
| SeaweedFS | Apache-2.0, very active, widely adopted. No MinIO admin API, no in-place read of MinIO data. | Long-term candidate, not for 0.6.0 |
| RustFS | Apache-2.0, easy to run. MinIO-format compatibility is preview and cannot read encrypted objects. Many 2026 advisories. CLA allows relicensing. | Not chosen |
| Garage | No object tagging, bucket policy or object lock. | Rejected: would disable takedown revocation and legal holds |
| Ceph RGW | Full S3 feature set. Cluster-shaped footprint. | Rejected: wrong size for a single-box app |
| versitygw | POSIX gateway with its own IAM. Lifecycle and tag-policy support unverified. | Not chosen |
| AIStor Free | Proprietary licence-key gated edition. | Rejected: not redistributable inside an AGPL product |

Zenko CloudServer (no arm64), LocalStack (a test emulator), Apache Ozone (multi-service JVM) and
the Bitnami image (gone) were also disqualified. Full table with image refs and probe results:
the alternatives report, section 2.

## What comparable projects chose

| Project | Choice | Reference |
|---|---|---|
| Grafana Loki | Silo (compose stacks and Helm) | [loki#25393](https://github.com/grafana/loki/pull/25393) |
| Grafana Mimir | Silo | [mimir#16604](https://github.com/grafana/mimir/pull/16604) |
| Argo Workflows | Silo | [argo-workflows#17073](https://github.com/argoproj/argo-workflows/pull/17073) |
| Plane | Silo | [plane#9963](https://github.com/makeplane/plane/pull/9963) |
| RAGFlow | Silo | [ragflow#18231](https://github.com/infiniflow/ragflow/pull/18231) |
| Coolify | Silo (one-click template) | `templates/compose/minio.yaml` in coollabsio/coolify |
| Testcontainers-java | Accepts Silo as a compatible image | [testcontainers-java#12113](https://github.com/testcontainers/testcontainers-java/pull/12113) |
| Langfuse | Chainguard MinIO | [langfuse#10585](https://github.com/langfuse/langfuse/pull/10585) |
| Supabase (self-hosted) | Chainguard MinIO | `docker-compose.s3.yml` in supabase/supabase |
| Mattermost | Chainguard MinIO | [mattermost#38495](https://github.com/mattermost/mattermost/pull/38495) |
| Kubeflow Pipelines | SeaweedFS (default since 2.15) | [pipelines#11965](https://github.com/kubeflow/pipelines/pull/11965) |
| Sentry self-hosted | SeaweedFS | [self-hosted#3498](https://github.com/getsentry/self-hosted/pull/3498) |
| PostHog | SeaweedFS, data copied to a new volume | [posthog#100887](https://github.com/PostHog/posthog/pull/100887) |
| Onyx | SeaweedFS, copy job | [onyx#15249](https://github.com/onyx-dot-app/onyx/pull/15249) |
| MLflow, LobeHub | RustFS (examples and dev stacks) | [mlflow#21099](https://github.com/mlflow/mlflow/pull/21099), [lobehub#19572](https://github.com/lobehub/lobehub/pull/19572) |
| Milvus, Airbyte, Budibase | Rehosted a frozen old MinIO | [milvus#53889](https://github.com/milvus-io/milvus/pull/53889) |

Projects that must keep their data in place mostly chose a patched MinIO fork. Every project that
left the MinIO on-disk format copied its data side by side into a new volume. None did it in place.

## Decision

For 0.6.0, the `minio` service runs
`docker.io/pgsty/silo:RELEASE.2026-10-26T00-00-00Z@sha256:7bb2aff6b89efe1cb1303c10fc21d579447434ed06649ac9ec05a5e2dbf30ad7`.
The service name, volume, ports and environment are unchanged, so existing installs upgrade
without moving any data.

After the release:

1. Mirror the pinned Silo digest into our own Docker Hub namespace under a non-"minio" name.
2. Make the backend S3-generic by putting the presign identity behind an interface.
3. Evaluate SeaweedFS as a non-MinIO default. Any move is an API-copy migration into a new volume,
   never in place.

## Consequences

- No `.env` change and no data copy for operators. The upgrade notes ask for a database backup and
  a media backup or snapshot first.
- Pull and offline-package image references change (`docker-compose.yml`, offline docs and
  scripts).
- We depend on a fork with a small maintainer base until the mirror and the S3-generic work land.
- Downgrading to old MinIO on a volume Silo has used is not guaranteed safe. Rollback means
  restoring a volume snapshot.
- AGPL attribution for MinIO and Silo is recorded in `NOTICE`.

## Risks

- **One dominant maintainer.** The large majority of Silo's commits since the fork are from one
  person, who says so plainly. The project commits to releases every one to two months and to
  public notice plus archiving if maintenance stops for six months. Mitigation: mirror the image
  into our namespace and keep a copy of the source for the pinned tag.
- **Why not mirror the old image.** It is the fastest fix and carries no data risk, but it
  re-ships published HIGH advisories that have no community fix. It stays an emergency fallback
  only.
- **Fallback if Silo stalls.** Chainguard maintains a company-staffed public fork
  (`chainguard-forks/minio`) of the same on-disk format with the same advisory fixes. Its tags are
  public AGPL source, so we could build it ourselves.
- **Unverified compatibility.** The in-place claim comes from PGSTY's documentation and is being
  tested against our data, including encrypted objects, before release.

## SeaweedFS adoption caveats

If we evaluate SeaweedFS as the long-term default, the survey found these to plan for:

- **Anonymous by default.** Without credentials it starts in an allow-all mode. Always set
  credentials.
- **Telemetry on by default.** The master reports to the project unless disabled explicitly.
- **Data directory defaults to `/tmp`** under `weed server`, which lost Sentry users' data on
  restart. Set the data directory explicitly.
- **Advisories.** Many 2026 advisories target its internal gRPC, volume, filer and SFTP surfaces.
  Publish only the S3 port.
- **Space reclamation.** Sentry users reported storage that did not shrink after deletes and
  cleanup. OpenTranscribe deletes media for takedowns, so vacuum behaviour must be rehearsed.
- **No in-place migration from MinIO.** A side-by-side copy is required.
- **Single-maintainer risk** is lower than Silo's but still present.

## Revisit

Revisit on 2027-04-30, or sooner if Silo misses its release cadence, publishes an unfixed HIGH
advisory, or archives the repository.
