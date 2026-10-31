# S3 object store: v0.6.0 unblock and longer-term replacement

Research date: **2026-10-31**. Branch inspected: `release/prep-0.6.0` @ `46bce654c`.
Related: issue #931 (open, re-evaluate the object store), #996 (closed, Docker Hub removal),
commit `750042fff` (repointed to quay.io for v0.5.0), #775 (accepted-risk register), #907
(presign identity).

Every registry claim below comes from a read-only anonymous probe run on 2026-10-31:
an anonymous bearer token from the registry's token endpoint, then `GET /v2/<repo>/manifests/<tag>`.
No image was pulled or run, and no container was started. Section 6 lists what could not be verified.

---

## 1. Recommendation

**For v0.6.0, switch the `minio` service to `docker.io/pgsty/silo:RELEASE.2026-10-26T00-00-00Z`,
pinned by digest (`sha256:7bb2aff6b89efe1cb1303c10fc21d579447434ed06649ac9ec05a5e2dbf30ad7`).**
Silo is PGSTY's AGPL-3.0 fork of community MinIO. It was called `pgsty/minio` until 2026-08-06.

- It pulls anonymously, as a multi-arch image (amd64 and arm64).
- It keeps the `MINIO_*` environment, the `/minio/*` routes (including the admin API that #907's
  presign identity uses) and MinIO's on-disk layout. The project says "no data copy or metadata
  rewrite is required". If that holds, existing installs upgrade in place and their media is not touched.
- Its release notes say it fixes the HIGH advisories that community MinIO never patched (§4.0).
  This is the deciding point. **Our current pin is not just unpullable, it is vulnerable.** Those
  fixes exist only in AIStor (MinIO's commercial line) and in forks. `minio/minio` is archived, and
  its last community tag is `RELEASE.2025-10-15T17-29-55Z`.
- So mirroring the old digest (option i) or building upstream source (option iii) would restore
  installs but **ship known-unpatched HIGH CVEs**.

Before tagging, the swap must pass:
- the fresh-install and upgrade rehearsals, with KMS auto-encryption **on** (the release-test env
  template does not turn it on today, §5.3);
- `tests/integration/test_presign_revocation.py`;
- the multipart and e2e upload suites.

**Rollback:** re-pin the old digest. It is still cached on every existing install, but nobody
should run it long-term. Snapshot the media volume, or confirm a media mirror, before upgrading.
Silo's own docs say a downgrade to old MinIO is not guaranteed safe (§4.2).

**After the release:**
1. Mirror the chosen Silo digest into our own Docker Hub namespace. Use a non-"minio" name for
   trademark hygiene, for example `davidamacey/opentranscribe-objectstore`. This removes the
   third-party-deletion risk that hit us twice.
2. Make the backend genuinely S3-generic: put the MinIO admin-API presign identity behind an
   interface, and stop depending on `mc` in the rehearsal and on MinIO-specific health paths.
   Then an operator can bring SeaweedFS, Ceph RGW or AWS.
3. Re-evaluate in about six months whether a non-MinIO default (SeaweedFS first, RustFS second)
   is worth a data migration. Silo has one dominant maintainer (§4.2), and that is the real
   long-term risk.

Do not move the default to Garage. It has no object tagging, no bucket or IAM policy and no
object lock, so it would silently disable takedown revocation (#907) and storage legal holds.

---

## 2. Candidate comparison

"Anon pull" means an anonymous token plus a manifest GET returned HTTP 200 on 2026-10-31, from this host.

| Candidate | Image ref (registry) | Licence | Latest release (date) | amd64 / arm64 | Anon pull verified | Compatibility verdict |
|---|---|---|---|---|---|---|
| **MinIO, current pin** | `quay.io/minio/minio:RELEASE.2025-09-07T16-13-09Z` | AGPL-3.0 | community line ended. Last source tag `RELEASE.2025-10-15T17-29-55Z` (2025-10-16), repo archived | both (3-child list, per `750042fff`) | **N**: quay **401 UNAUTHORIZED**. Docker Hub **401**. API returns `Requires authentication` | n/a: unpullable **and** carries unpatched HIGH CVEs (§4.0) |
| MinIO `mc` | `quay.io/minio/mc:latest` | AGPL-3.0 | none since Sep 2025 | n/a | **N** (401) | only the rehearsal and docs use it |
| **PGSTY Silo** (ex-`pgsty/minio`) | `docker.io/pgsty/silo:RELEASE.2026-10-26T00-00-00Z` (also a `-distroless` variant) | AGPL-3.0 (label `AGPL-3.0-or-later`) | RELEASE.2026-10-26 (2026-10-26). Repo pushed 2026-10-31 | both | **Y** | **Drop-in, claimed.** Same env, routes and on-disk format. Re-test the tag-conditioned policy, presigned PUT, and mc→mcli |
| `pgsty/minio` (old name) | `docker.io/pgsty/minio:RELEASE.2026-08-04T00-00-00Z` | AGPL-3.0 | frozen at 2026-08-04 (rename) | both | **Y** | same lineage, but no fixes after the rename. Do not pick |
| Chainguard MinIO | `cgr.dev/chainguard/minio:latest` | AGPL-3.0 content. Chainguard free-tier terms not reviewed | built 2026-10-29. Upstream version **unknown** | both | **Y** (`:latest`/`latest-dev` only. Version tags 404) | **Needs config.** Runs as UID 65532 (existing root-owned `/data` would need a chown), no curl or shell (healthcheck breaks), cannot pin a version tag. Whether the 2026 CVE fixes are in it is unverified |
| SeaweedFS | `docker.io/chrislusf/seaweedfs:4.51` | Apache-2.0 | 4.51 (2026-10-25). Very active | both (+386/arm/ppc64le/s390x) | **Y** | **Needs code change.** No MinIO admin API, so the #907 identity must be rebuilt on its IAM API. Tagging, object lock, bucket policy, multipart, presign, CORS and SSE are listed as supported. Data via S3 copy |
| RustFS | `docker.io/rustfs/rustfs:1.0.4` | Apache-2.0 | 1.0.4 (2026-10-25). 1.0 GA is recent | both | **Y** | **Needs code change / risky.** `mc admin user svcacct add` reportedly works (rustfs#5061), but policy-semantics gaps are reported. "MinIO on-disk compatibility" is **Preview**, off by default, and **cannot read MinIO-encrypted objects**. Data via S3 copy |
| Garage | `docker.io/dxflrs/garage:v2.5.0` | AGPL-3.0 | v2.5.0 (2026-10-24) | both (+386/arm) | **Y** | **Needs code change, loses features.** No object tagging, no bucket policy, no object lock, no versioning. Lifecycle is Expiration and AbortIncompleteMultipart only. #907 revocation and legal hold become impossible |
| versitygw | `docker.io/versity/versitygw:v1.10.0` | Apache-2.0 | v1.10.0 (2026-10-22) | both | **Y** | **Needs code change.** POSIX-backed gateway with its own IAM, no MinIO admin API. Lifecycle and tag-conditioned policy support **not verified** |
| Ceph RGW | `quay.io/ceph/ceph:v19` | LGPL-2.1/3 (mostly) | Squid v19 line | both | **Y** | **Needs code change, heavy.** Full S3 feature set, but MON+MGR+OSD+RGW is a cluster-shaped footprint (GBs of RAM) for a single-box app. Wrong fit as the default |
| Zenko CloudServer | `ghcr.io/scality/cloudserver:9.5.2`. Docker Hub `zenko/cloudserver` last updated 2022 | Apache-2.0 | 9.5.2 (2026-10-27) | **amd64 only** (ghcr), single-arch (Hub) | **Y** | **Disqualified:** no arm64 |
| LocalStack | `docker.io/localstack/localstack:latest` | Apache-2.0 core | n/a | both | **Y** | **Disqualified:** a test emulator, not a durable store. Recent auth-token/licensing changes **not verified** |
| Apache Ozone | `docker.io/apache/ozone:latest` | Apache-2.0 | ozone-2.2.2 (2026-10-30) | both | **Y** | **Disqualified:** multi-service JVM (OM/SCM/DN/S3G) footprint |
| Bitnami MinIO | `docker.io/bitnami/minio:latest` | n/a | n/a | n/a | **N** (404, tag unknown) | gone (Bitnami catalog change) |
| OpenMaxIO | `github.com/OpenMaxIO/openmaxio-object-browser` | AGPL-3.0 | last push 2025-06 | n/a | no server image | console fork only, **not a server** |
| AIStor Free | min.io/download | proprietary free licence (not AGPL) | n/a | n/a | not probed | **Disqualified:** licence-key gated, not redistributable inside an AGPL product |

Memory and image size. The compressed sizes come from the Hub API or manifests:
- Silo: 58 MB (amd64 standard), 36 MB (distroless).
- Chainguard: 64 MB.
- SeaweedFS: about 198 MB (`dev` tag).
- RustFS: 115 MB.
- Garage: about 27 MB.
- versitygw: 36 MB.
- Current MinIO: 175 MB uncompressed (local `docker image ls`).

Idle RSS was **not measured** for any candidate. The "heavy" verdicts for Ceph and Ozone rest on
their architecture, not on a measurement.

---

## 3. What OpenTranscribe actually uses from S3

The client is always `minio.Minio` (minio-py, a generic S3 SDK). boto3 appears once, for CORS.
Line numbers are against `46bce654c`.

| Feature | Where | Portable? |
|---|---|---|
| Bucket create / exists (no object-lock, no versioning at creation) | `backend/app/services/minio_service.py:51-59`, `:717-728` | generic |
| PutObject / GetObject / fget / stat / remove | `minio_service.py:78, 110, 140, 225, 353, 378, 388, 405, 546, 593, 628-713` | generic |
| **Range GET** | `minio_service.py:555-577` (`range_read`) | generic |
| Batch delete (`remove_objects`), recursive list | `minio_service.py:805-836` | generic |
| **Presigned GET with `response-content-type`** | `minio_service.py:143-200` (`get_file_url`) | generic SigV4 |
| **Presigned GET with `response-content-disposition`** (downloads) | `minio_service.py:411-452` | generic SigV4. Some stores ignore `response-*`, so test |
| Internal presigned GET (worker side) | `minio_service.py:455-470` | generic |
| **Presigned PUT (browser upload)** | `minio_service.py:489-530`, `multipart_upload.py:270-302` | generic SigV4 |
| **Browser-side multipart**: `_create_multipart_upload`, presigned part URLs (`partNumber`/`uploadId` via `extra_query_params`), `_list_parts`, `_complete_multipart_upload`, `_abort_multipart_upload`, `_list_multipart_uploads` | `backend/app/services/multipart_upload.py:83, 106-111, 126, 158, 165, 185` | generic (Garage, SeaweedFS and RustFS all list these) |
| Lifecycle: prefix Expiration rules (bulk exports etc.) | `minio_service.py:731-803` | generic. Garage needs the prefix in `Filter` (it already is) |
| Lifecycle: AbortIncompleteMultipartUpload, **native S3 only** (MinIO ILM rejects it) | `multipart_upload.py:205-249` | backend-specific branch already exists |
| **Object tagging** (get + set, quarantine tag) | `minio_service.py:266-315`. Backfill at `backend/app/main.py:302-330, 407-414` | **Garage: missing** |
| **Object legal hold** (best-effort, needs bucket object-lock, which the bundled bucket does NOT enable, so today it is a logged no-op) | `minio_service.py:230-263`. Callers: `takedown_service.py:344, 509`, `content_hold_service.py:225-227`, `media_collections.py:1185` | Garage: missing. Effectively inert today anyway |
| **MinIO admin API**: `MinioAdmin.add_service_account` / `update_service_account` with an inline IAM policy (Allow GetBucketLocation + GetObject, **Deny GetObject on `s3:ExistingObjectTag/<key>="true"`**) | `backend/app/services/storage_presign_identity.py:120-175` (policy), `:246-288` (admin calls), `:291-333` (restricted client) | **MinIO lineage only.** Fails open to the root signer elsewhere (`:281-288`) |
| Bucket policy read (native S3 revocation posture) | `storage_presign_identity.py:400-449` | generic. Garage has no policy API |
| Bucket CORS via boto3 (opt-in, native S3) | `backend/app/services/storage_backend.py:320-350` | generic |
| Path-style addressing, presigned host rewritten to `/s3` | `storage_backend.py:160-197`. Proxies at `frontend/nginx.conf:191-221`, `frontend/nginx-pki.conf:200-221, 454-475`, `frontend/vite.config.ts:105-141` (all `proxy_set_header Host minio:9000`) | generic, as long as the store accepts path-style requests signed for host `minio:9000`. **Keep the service name `minio`** in any swap, or every presigned URL breaks |
| Server-side encryption at rest: `MINIO_KMS_SECRET_KEY` + `MINIO_KMS_AUTO_ENCRYPTION` (`on` in `.env.example:27, 96`) | `docker-compose.yml:76-81` | **MinIO lineage only.** Encrypted objects are unreadable by any other store's disk reader (RustFS says so explicitly) |
| No-phone-home env (`MINIO_UPDATE`, `MINIO_CALLHOME_ENABLE`) | `docker-compose.yml:82-86` | Silo behaviour unverified |
| Healthcheck `curl -f http://localhost:9000/minio/health/live` (needs curl in the image) | `docker-compose.yml:88-95`, `opentr.sh:4068, 4109` | MinIO-lineage path. Silo standard image ships curl. Distroless and Chainguard do not |
| Console on :9001 behind `/minio/` | `docker-compose.yml:74, 87`, nginx `location ^~ /minio/` | cosmetic |
| `mc` **inside the MinIO container** (ETag snapshot that proves no object mutation across an upgrade) | `scripts/release-tests/test-upgrade.sh:1058-1062, 1084-1090` | Silo ships `mcli` with an `mc` alias (claimed). Fails soft to `[]` otherwise |
| `mc` in operator docs | `docs-site/docs/operations/backup-restore.md:314-333, 454-488`, `production-deployment.md:338-339` | docs only. `quay.io/minio/mc` in docs is now unpullable too |
| Image pin references | `docker-compose.yml:62`, `scripts/uninstall-offline-package.sh:269, 291`, `README-OFFLINE.md:598`, `docs-site/docs/installation/offline-installation.md:60, 70`, `backend/tests/unit/test_minio_image_registry.py` | must move together |

Settings surface (`backend/app/core/config.py`): `MINIO_ROOT_USER/PASSWORD/HOST/PORT/SECURE` at
`:522-526`, `STORAGE_BACKEND` (`minio|s3`) at `:551`, `S3_*` at `:554-578`,
`MINIO_PUBLIC_URL`/`STORAGE_PUBLIC_URL` at `:597-603`, and `STORAGE_PRESIGN_IDENTITY_ENABLED` at `:632`.

Blast radius:
- 57 files under `backend/app` import `minio_service` or `minio`.
- 154 files under `backend/tests` mention minio. The heaviest are `test_opentr_minio_kms_first_run.py`,
  `test_storage_backend.py`, `test_files_prepare_complete_upload.py`,
  `test_user_delete_storage_reclaim.py`, `test_multipart_upload.py` and
  `integration/test_presign_revocation.py`.
- 9 files under `scripts/release-tests`. `test-upgrade.sh` alone has 30 mentions.

**Verdict on the feature set.** The S3 data plane is portable. Three things are MinIO-lineage-specific:
1. the #907 presign identity (admin API);
2. KMS-encrypted data at rest;
3. small operational dependencies (`/minio/health/live`, `mc` inside the container).

Any non-MinIO store therefore needs code for (1) and an S3-API data copy for (2).

---

## 4. Short-term options for v0.6.0

### 4.0 Why "just restore the old image" is not enough

From the GitHub security advisories on `minio/minio`, read 2026-10-31. The vulnerable ranges all
include `RELEASE.2025-09-07T16-13-09Z`:

| Advisory | Severity | Patched in | In community source? |
|---|---|---|---|
| GHSA-jjjj-jwhf-8rgr / CVE-2025-62506: service-account / STS session-policy bypass, privilege escalation | high | `RELEASE.2025-10-15T17-29-55Z` | yes (source-only tag, never published as an image) |
| GHSA-hv4r-mvr4-25vw / CVE-2026-41145: unauthenticated object write via unsigned-trailer query auth | high | `>= RELEASE.2026-04-11T03-20-12Z` | **no** (AIStor / forks) |
| GHSA-9c4q-hq6p-c237 / CVE-2026-40344: unauthenticated object write via Snowball auto-extract | high | `>= RELEASE.2026-04-11T03-20-12Z` | **no** |
| GHSA-3rh2-v3gr-35p9 / CVE-2026-34204: SSE metadata injection, objects become permanently unreadable | high | `>= RELEASE.2026-03-26T21-24-40Z` | **no** |
| GHSA-xh8f-g2qw-gcm7 / CVE-2026-42600: ReadMultiple path traversal | medium | 2026-04-14 | **no**. Multi-node only, so single-node OT is not affected |

The `minio/minio` commit log after 2025-10-16 holds only README and doc changes plus four small
fixes (last real fix 2025-10-24). The README now says "THIS REPOSITORY IS NO LONGER MAINTAINED"
and points to AIStor. This **confirms #931's premise** with a vendor statement: community
distribution is source-only, and the source line is frozen.

An OpenTranscribe-specific exposure assessment of these advisories (how the app's own URL surface
interacts with their preconditions) has been reported to the owner separately. It is deliberately
not recorded in this public-repo file.

### 4.1 Option (i): mirror the cached digest into `davidamacey/`

- **Speed:** minutes. `docker tag` the cached
  `sha256:14cea493…` image, push it, and change one compose line. The build host still holds it
  (`docker image ls --digests`). Note: `docker tag` + `push` of a local image pushes only this
  host's architecture. To keep both arches, use `docker buildx imagetools create` from a source
  that still serves the full index (none does now), or `docker save`/`load` per arch from caches
  that hold both. **Only the amd64 child is confirmed present locally.**
- **Data risk:** none. Identical binary.
- **Security:** ships every advisory in §4.0. This is not acceptable as the v0.6.0 default, at
  most an emergency fallback.
- **Licence obligations (AGPL-3.0 §6):**
  - Conveying the object code from a network server requires offering the Corresponding Source.
    §6(d) allows the source to sit on a different server "provided you maintain clear directions
    next to the object code", and you "remain obligated to ensure that it is available".
  - Put the directions in the Docker Hub description and in OCI labels:
    `org.opencontainers.image.source=https://github.com/minio/minio`, plus `revision` = the
    release commit.
  - Because the upstream repo is archived and the vendor has already deleted artifacts twice,
    **keep our own copy of the source tarball** for that tag, for example as a release asset in
    an attevon-llc repo. Pointing only at upstream is a liability.
  - Keep the image's `/licenses` (LICENSE, CREDITS) intact.
  - The image also contains `mc` (AGPL, same treatment), `curl` (curl licence) and a Red Hat UBI
    base. The UBI EULA permits redistribution; I did not re-read it for this report.
- **Trademark:** AGPL grants no trademark rights. Silo renamed away from "MinIO" for exactly this
  kind of reason. Avoid `davidamacey/minio`.
- **Existing gap, independent of this decision:** `scripts/build-offline-package.sh` already
  `docker save`s the MinIO image into the offline tarball (`:116-127`). That **is** conveyance,
  and the repo `NOTICE` names no MinIO, mc or UBI attribution or source offer. Fix this whichever
  store ships, for whichever image the offline package carries.

### 4.2 Option (ii): maintained fork, PGSTY Silo (recommended)

- **Image:** `docker.io/pgsty/silo:RELEASE.2026-10-26T00-00-00Z`. The index digest is
  `sha256:7bb2aff6b89efe1cb1303c10fc21d579447434ed06649ac9ec05a5e2dbf30ad7`, with amd64 and arm64
  children plus an attestation entry. Base `ubi9-micro`, runs as root (same as upstream, so no
  `/data` chown is needed). Image history shows curl and mcli copied in, and the
  `/licenses/{LICENSE,NOTICE,CREDITS}` that a redistributor needs.
- **Entrypoint:** `docker-entrypoint.sh` maps a first argument of `server`/`minio` onto `silo`.
  Our `command: server /data --console-address ":9001"` therefore keeps working unchanged.
  Verified from `dockerscripts/docker-entrypoint.sh` on GitHub, not by running it.
- **Compatibility claims**, from silo.pgsty.com/compatibility/server:
  - `MINIO_*` env retained, with "no parallel SILO_* namespace".
  - `/minio/*` routes and the admin API retained.
  - "No data copy or metadata rewrite is required when the same disks move from MinIO to Silo."
  - KMS/KES configuration retained.
  - `/usr/bin/mc -> mcli` alias.
- **Security:** release notes for 2026-04-17, 06-18, 08-04, 09-16 and 10-18 describe fixes for
  the unsigned-trailer, Snowball, replication-metadata, OIDC and LDAP issues, and "Restricted
  credentials cannot create broader service accounts" (the CVE-2025-62506 class).
- **Behaviour changes to re-test against OT:**
  - Stricter SigV4: unsigned `x-amz-*` operation headers rejected, header-bound presigned
    payloads verified. Affects the browser's presigned PUT and multipart part PUTs.
  - "Re-test tag-conditioned write policies". Our policy is tag-conditioned on **GetObject**,
    which the page says keeps the inherited behaviour. Run `test_presign_revocation.py` anyway.
  - Service-account mutation rules tightened. We create and refresh as root, so this is expected
    to be unaffected.
- **Risks:**
  - Bus factor. Commits are dominated by inherited upstream authors; the active maintainer is
    essentially one person (Vonng / Pigsty), who runs it as Pigsty's backup store.
  - Silo states that an image-only rollback to an old MinIO image is "not safe" for its Helm
    chart (chart changes UID and paths). For our docker image, which keeps root and `/data`,
    rollback safety is **unverified**.
- **Licence:** AGPL-3.0, the same as OT. Redistribution obligations are as in 4.1 if we later
  mirror it.
- **Effort:**
  - Change `docker-compose.yml:62` and its comment block.
  - Update the four doc/script pin references and `test_minio_image_registry.py`'s docstring.
    Its regex still passes, since the ref is not `minio/…`. Consider extending it to assert the
    digest pin.
  - Run the gates: fresh-install rehearsal, upgrade rehearsal (FROM v0.5.x MinIO → TO Silo, with
    KMS on), `test_presign_revocation.py`, `test_multipart_upload.py`, e2e upload/download.
  - Roughly half a day to one day, mostly rehearsal wall-clock.

### 4.3 Option (iii): build from AGPL source in our own image build

- Upstream MinIO's README instructs exactly this
  (`go install github.com/minio/minio@RELEASE.2025-10-15T17-29-55Z`, or `make docker` from the tag).
- A minimal multi-stage Dockerfile: `golang:1.24-…` builder with `CGO_ENABLED=0 go build` at the
  tag, then a slim runtime holding the binary, curl (for the healthcheck), a non-root user
  (needs a `/data` chown on upgrade, a data-path risk) or root, and the LICENSE/CREDITS.
- Build time is minutes per arch natively. arm64 must use `USE_REMOTE_BUILDER=true` per repo rules.
- **I did not verify that the tag still builds.** The module graph is pinned by go.sum, so it
  likely does, but proxy availability of every dependency was not checked.
- **Security:** fixes CVE-2025-62506 only. The 2026 advisories stay open, so this is strictly
  worse than Silo at higher maintenance cost. Building **Silo** from source is the sensible form
  of this option if registry independence matters. Silo's Dockerfile.goreleaser and Makefile are
  in its repo.

### 4.4 Option (iv): a different store now

Rejected for v0.6.0:
- Every non-MinIO store needs the #907 code change.
- For any install with `MINIO_KMS_AUTO_ENCRYPTION=on` (the `.env.example` default), it also needs
  an S3-API data copy of every user's media.
- That is a migration, not an unblock, and it must not be rushed into a release.

### 4.5 Ranking for v0.6.0

| Rank | Option | Speed | Data risk | Security |
|---|---|---|---|---|
| 1 | **Silo by digest** | hours + rehearsal | low (claimed in-place compatible; prove with the upgrade rehearsal) | patched |
| 2 | Mirror the old digest (emergency only) | minutes | none | **known HIGH CVEs** |
| 3 | Build Silo from source into `davidamacey/` | 1-2 days | as rank 1 | patched, registry-independent |
| 4 | Build upstream MinIO 2025-10-15 | 1 day | very low | partially patched |
| 5 | Non-MinIO store | weeks | **migration required** | depends |

**Rollback plan for rank 1:**
1. Before the upgrade, the release notes instruct operators to take a media backup or volume
   snapshot. Run the existing `./opentr.sh backup`; the media mirror is covered in
   `backup-restore.md`.
2. If Silo misbehaves, re-pin `quay.io/minio/minio@sha256:14cea493…`. It is cached on every
   pre-existing install. New installs have no data to roll back, and could get option (i)'s
   mirror if one exists.
3. Rehearse that rollback once in a `--fresh` stack before release. It is the unverified link.

---

## 5. Migration outline

### 5.1 v0.6.0: MinIO → Silo, in place

- **Compose:**
  - `image: docker.io/pgsty/silo:RELEASE.2026-10-26T00-00-00Z@sha256:7bb2aff6…`
  - Keep `container_name: opentranscribe-minio` and the service name `minio`. Proxies and
    presigned hosts depend on `minio:9000`.
  - Keep the `minio_data:/data` volume and the NAS bind path (`docker-compose.nas.yml:26-28`).
  - Keep the command and healthcheck. The standard (non-distroless) image is required for curl.
- **Env:** no changes. All `MINIO_*` are kept. Check whether `MINIO_UPDATE=off` still suppresses
  update checks under Silo (DNS-log check as was done for MinIO).
- **Data:** none moved. Before and after the swap, verify by the ETag snapshot in
  `test-upgrade.sh`, plus a download of a KMS-encrypted object.
- **Offline package:** `build-offline-package.sh` reads images from `docker-compose.yml`, so it
  follows automatically. Update `uninstall-offline-package.sh:269, 291`, `README-OFFLINE.md:598`
  and `offline-installation.md:60, 70`.
- **Docs:** replace `quay.io/minio/mc` in `backup-restore.md:458` with `pgsty/mc` (anonymous pull
  verified) or `docker compose exec minio mc …`. Update the changelog and the security note.
- **Tests:**
  - `test_minio_image_registry.py` still passes, but its docstring and the asserted rationale
    need rewriting.
  - Run `test_presign_revocation.py` live.
  - Add KMS auto-encryption to `scripts/release-tests/lib/env-template.sh`. Today it sets no
    `MINIO_KMS_*`, so the upgrade rehearsal **never exercises encrypted objects**, the most
    common real-world data shape.
- **#775:** record the Silo single-maintainer dependency as an accepted risk.

### 5.2 Post-release: decouple and mirror

1. Mirror the Silo digest into `davidamacey/opentranscribe-objectstore`, keeping both arches with
   `docker buildx imagetools create --tag … docker.io/pgsty/silo@sha256:7bb2…`. Add the AGPL §6(d)
   source directions and a retained source tarball. Add a MinIO/Silo/mc/UBI section to `NOTICE`.
2. Add a `PresignIdentityProvider` seam with three implementations:
   - MinIO-lineage admin API (today's code);
   - S3-IAM (SeaweedFS and Ceph RGW IAM APIs, for an identity with an inline policy);
   - bucket-policy-only (AWS, today's `report_native_s3_revocation_posture` path).
3. Replace `mc` in `test-upgrade.sh` with the backend container's minio-py, or boto3 as
   `setup-watch-source-test-data.sh:87-109` already does. Replace `/minio/health/live` with a
   backend-agnostic probe (an S3 `HeadBucket`, or a per-backend healthcheck in compose).

Effort: about 3-5 days including tests.

### 5.3 If a non-MinIO default is ever chosen (SeaweedFS is the front-runner)

**Never in place.** No other store reads MinIO's `xl.meta` + KMS-encrypted layout (RustFS: preview
only, and not encrypted objects). The process:

1. Start the new store **beside** MinIO/Silo on a new volume. Never reuse `minio_data` or the NAS path.
2. Copy through the S3 API while MinIO is serving, because MinIO decrypts transparently. Use
   `rclone sync` or minio-py from the backend container, for both buckets (`opentranscribe`, and
   the cache bucket `CACHE_BUCKET_NAME`). Content-Type must be preserved.
3. Verify every object by count, size and **SHA-256 of the body**. ETags of multipart objects
   are not comparable across stores. Fail closed on any mismatch.
4. Rebuild metadata from Postgres, which is already the source of truth for all of it:
   - quarantine tags: `main.py:_backfill_quarantine_tags`, which needs to become unbounded for a
     migration;
   - legal holds: the `MediaFile.legal_hold` / content holds;
   - lifecycle rules: `ensure_prefix_expiry` at startup;
   - the presign identity: re-provisioned at startup.
5. Cut over with an env and compose change (`MINIO_HOST`, or keep the service name `minio`
   pointed at the new container). Keep the old volume **read-only and untouched for at least one
   release** as the rollback.
6. Gates: the full rehearsal matrix, plus a migration rehearsal on a seeded `--fresh` stack with
   KMS on.

Effort: 2-3 weeks including the #907 provider work and a migration tool with its own tests. The
test-suite impact is mostly the 154 test files that name minio, many through fixtures in
`backend/tests/conftest.py`. Most are about the SDK and would not change; the store-specific ones
are the presign, KMS and image-registry tests.

---

## 6. Claims I could not verify

- **Silo in-place compatibility with *our* data.** The "no data copy or metadata rewrite" claim
  comes from PGSTY's docs. Their audit baseline is upstream MinIO from 2026-02-11, not
  2025-09-07. Not tested here: no containers were started.
- That Silo decrypts objects written by MinIO 2025-09-07 with `MINIO_KMS_SECRET_KEY` auto-encryption.
- That `/minio/health/live` answers on Silo. The page says `/minio/*` is retained but does not
  name this path.
- That `mc` exists in the Silo standard image. The compatibility page says `/usr/bin/mc -> mcli`.
  The image history shows `ln -sf mcli /usr/bin/`, which is ambiguous.
- That `mcli ls --json` output matches `mc`'s, which `test-upgrade.sh` parses.
- That the browser presigned PUT, multipart part PUTs, `response-content-disposition`, and the
  #907 tag-conditioned Deny all behave identically under Silo's stricter SigV4/policy evaluation.
- Rollback safety from Silo back to MinIO 2025-09-07 on the same volume.
- Whether Silo phones home, and whether `MINIO_UPDATE`/`MINIO_CALLHOME_ENABLE` still apply.
- Chainguard MinIO: which upstream version it builds, whether it carries the 2026 CVE fixes, how
  long old digests stay retrievable, and the free-tier redistribution terms.
- RustFS policy semantics for `s3:ExistingObjectTag` Deny on GetObject. Only an issue thread
  shows `mc admin user svcacct` working.
- SeaweedFS support for `s3:ExistingObjectTag` conditions, and prefix-filtered lifecycle Expiration.
- versitygw lifecycle and tag-conditioned policy support.
- LocalStack's current auth-token or licence requirements.
- Idle memory footprint of every candidate (not measured).
- That the pinned MinIO tag still builds from source with today's Go module proxy.
- That an arm64 child of `sha256:14cea493…` is cached anywhere we control. Option (i) may be
  amd64-only.
- The UBI EULA redistribution terms (not re-read for this report). The AGPL §6 reading here is an
  engineering summary, not legal advice.
