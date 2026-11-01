# S3 object store after MinIO: what other projects actually did

Research date: **2026-10-31**. This is the companion to
[`s3_object_store_alternatives_v060.md`](s3_object_store_alternatives_v060.md), which covers
registry probes and the feature inventory. That work is not repeated here.

**Method.** The primary evidence is each project's *current* compose, Helm or test file, read
directly from GitHub, plus the commit or PR that changed it (with merge dates from the GitHub
API). Docker Hub pull counts come from `hub.docker.com/v2/repositories/<repo>/`. GitHub stats and
security-advisory counts come from the GitHub API. Forum and blog material supports this but
does not replace it.

How claims are labelled:
- **VERIFIED**: I read the primary source (the file, the commit, the PR, or the project's docs).
- **REPORTED**: the claim comes from a secondary source (a blog, a forum, or a search summary)
  and I did not confirm it at the source.

No images were pulled and no containers were run.

---

## (a) Bottom line (≈150 words)

There is no single successor. Major projects split three ways:

1. **Keep MinIO via a patched fork.** This is the most common choice for projects that must not
   migrate data:
   - **PGSTY Silo**: Grafana Loki, Grafana Mimir, Argo Workflows, Plane, RAGFlow, the Coolify
     template, GitLab QA (MR still open) and Testcontainers-java.
   - **Chainguard's `cgr.dev/chainguard/minio`**: Langfuse, Supabase self-hosted and Mattermost.
2. **Move to SeaweedFS.** This is the dominant *non-MinIO* choice: Kubeflow Pipelines (default
   since KFP 2.15), Sentry self-hosted, PostHog, Onyx and Thanos e2e.
3. **Move to RustFS**, mostly in dev or example stacks: MLflow, LobeHub and Akamai's Milvus app.

Several projects instead **rehosted a frozen old MinIO** under their own namespace (Milvus,
Airbyte, Budibase), which carries the unpatched CVEs.

**Recommendation: unchanged, and strengthened.** Silo for v0.6.0 is what the largest peers chose
in the same situation. SeaweedFS is the long-term non-MinIO pick that the most serious
self-hosted apps (Sentry, PostHog, Kubeflow) actually shipped.

**One addition.** Chainguard maintains a *company-staffed* patched fork,
`chainguard-forks/minio`. It carries the same HIGH CVE fixes, and its tags are public AGPL
source. That makes it a credible second source for the MinIO on-disk format if Silo's
single-maintainer risk materialises.

---

## (b) What major projects did

Dates are merge or commit dates in UTC. "Current file" means the file was read on 2026-10-31.

| Project | What they did | Date | Evidence | V/R |
|---|---|---|---|---|
| **Kubeflow Pipelines** | Default object store changed from MinIO to **SeaweedFS**. MinIO "remains fully supported"; legacy manifests "may be subject to removal". Default pipeline root is still `minio://…` | KFP PR merged 2025-08-20; manifests PR 2025-09-10; KF 1.11 released 2025-12-15 | [kubeflow/pipelines#11965](https://github.com/kubeflow/pipelines/pull/11965), [kubeflow/manifests#3240](https://github.com/kubeflow/manifests/pull/3240), [KFP object-store docs](https://www.kubeflow.org/docs/components/pipelines/operator-guides/configure-object-store/), [KF 1.11.0 release](https://github.com/kubeflow/community-distribution/releases/tag/v1.11.0) | VERIFIED |
| **Sentry self-hosted** | Added a **SeaweedFS** S3 nodestore. It now runs `weed mini` from `chrislusf/seaweedfs:4.51_large_disk`, pinned by digest | PR merged 2025-09-13; shipped in 25.9.0 (2025-09-17); bumped to 4.51 on 2026-10-27 | [getsentry/self-hosted#3498](https://github.com/getsentry/self-hosted/pull/3498), [release 25.9.0](https://github.com/getsentry/self-hosted/releases/tag/25.9.0), current `docker-compose.yml` | VERIFIED |
| **PostHog (hobby self-host)** | Replaced MinIO with **SeaweedFS** on a new volume. Interim step: quay.io (2026-09-14). Migration copies old data through a temporary MinIO container (`bin/migrate-storage-hobby`), because "SeaweedFS cannot read the MinIO on-disk format" | 2026-09-16 (dev/CI had moved on 2026-06-10, #60432) | [PostHog/posthog#100887](https://github.com/PostHog/posthog/pull/100887), current `docker-compose.hobby.yml` | VERIFIED |
| **Onyx** | Replaced bundled MinIO with **SeaweedFS** (`chrislusf/seaweedfs:4.47` by digest). Rollout: dual-write and fallback first, then a copy job, with MinIO kept running during the copy (image rehosted as `onyxdotapp/minio`) | 2026-09-30 | [onyx#15249](https://github.com/onyx-dot-app/onyx/pull/15249), [onyx#15250](https://github.com/onyx-dot-app/onyx/pull/15250), [Onyx object-store docs](https://docs.onyx.app/deployment/local/object_store) | VERIFIED |
| **Thanos** (e2e) | Ported its e2e object storage from MinIO to **SeaweedFS**. Its harness library `efficientgo/e2e` gained a SeaweedFS runnable | 2026-09-21; efficientgo 2026-09-27 | [thanos#9037](https://github.com/thanos-io/thanos/pull/9037), [efficientgo/e2e#79](https://github.com/efficientgo/e2e/commits/main/db/db.go) | VERIFIED |
| **Grafana Loki** | Four docker-compose stacks moved to **`pgsty/silo`**, pinned by tag and digest. The PR text says "The Helm chart already runs the same fork"; Helm `values.yaml` overrides to `docker.io/pgsty/silo` + `pgsty/mc` "to mitigate the unresolved CVE" | compose 2026-10-20; Helm change date not found | [grafana/loki#25393](https://github.com/grafana/loki/pull/25393), current `production/helm/loki/values.yaml` | VERIFIED |
| **Grafana Mimir** (dev env and integration test) | Moved from quay.io (2026-09-14) to **`pgsty/silo:RELEASE.2026-09-03T13-18-01Z`** | 2026-09-18 | [grafana/mimir#16604](https://github.com/grafana/mimir/pull/16604), current `development/mimir-monolithic-mode/docker-compose.yml` | VERIFIED |
| **Argo Workflows** (quick-start) | Artifact repository moved to **`pgsty/silo`** by digest | 2026-09-25 | [argo-workflows#17073](https://github.com/argoproj/argo-workflows/pull/17073) | VERIFIED |
| **Plane** | quay.io (2026-09-15), then **`pgsty/silo:RELEASE.2026-09-16`** on master and in release v1.4.3. The commercial Helm docs reportedly default to `pgsty/minio` | 2026-10-08 | [makeplane/plane#9963](https://github.com/makeplane/plane/pull/9963), commit `bab49bb97` (#9962), current community compose | VERIFIED (Helm: REPORTED) |
| **RAGFlow** | MinIO → `pgsty/minio` → **`pgsty/silo`** | 2026-04-02, then 2026-08-13 | [ragflow#13896](https://github.com/infiniflow/ragflow/pull/13896), [ragflow#18231](https://github.com/infiniflow/ragflow/pull/18231) | VERIFIED |
| **Coolify** (one-click template) | **`pgsty/silo:RELEASE.2026-10-15T00-00-00Z`**. The tag exists on Hub. The template had been disabled on 2025-10-21 | 2026-10-15 | `coollabsio/coolify` `templates/compose/minio.yaml` history | VERIFIED |
| **GitLab** | The Helm chart **dropped its bundled MinIO** (a breaking change; external object storage is now required). It had used mirrored MinIO images since 2025-12-05. A GitLab QA MR proposes **`pgsty/silo`** for object-storage tests, but it is **still open** | chart 2026-05-11; QA MR opened 2026-10-06 | [charts commit 26820c49](https://gitlab.com/gitlab-org/charts/gitlab/-/commit/26820c4955e839d5c3af080307c4006951218bd6), [gitlab-qa!1463](https://gitlab.com/gitlab-org/gitlab-qa/-/merge_requests/1463) | VERIFIED. GDK not checked |
| **Testcontainers-java** | `MinIOContainer` now accepts **`pgsty/silo`** as compatible. The default is still `minio/minio`. testcontainers-go tests pin `quay.io/minio/minio:RELEASE.2024-01-16…` | 2026-10-17 | [testcontainers-java#12113](https://github.com/testcontainers/testcontainers-java/pull/12113) | VERIFIED |
| **Langfuse** | **`cgr.dev/chainguard/minio`**, unpinned, in the official compose | 2025-12-01 | [langfuse#10585](https://github.com/langfuse/langfuse/pull/10585), current `docker-compose.yml` | VERIFIED |
| **Supabase** (self-hosted `docker-compose.s3.yml`) | **`cgr.dev/chainguard/minio`** + `chainguard/minio-client:latest-dev` | 2026-02-18 | supabase commit `b9ec8927c`, current file | VERIFIED |
| Supabase CLI (local dev) | Serves S3 from its own storage API (`…:54321/storage/v1/s3`), so no MinIO container is needed | n/a | [gist example](https://gist.github.com/eno-conan/29189268f51bcae19725cdc9ddd6e40b) | REPORTED |
| **Mattermost** (dev/test compose) | **`cgr.dev/chainguard/minio:latest`** | 2026-09-12 | [mattermost#38495](https://github.com/mattermost/mattermost/pull/38495) | VERIFIED |
| **MLflow** (docker-compose example) | MinIO example removed, **RustFS** added (`rustfs/rustfs:1.0.0-alpha.83`, which is inside the affected range of RustFS advisories fixed in alpha.94 and beta.11) | 2026-02-26 | [mlflow#21099](https://github.com/mlflow/mlflow/pull/21099) | VERIFIED (CVE range: REPORTED) |
| **LobeHub** (dev compose) | **`rustfs/rustfs:latest`** + `rustfs/rc` | 2026-09-15 | [lobehub#19572](https://github.com/lobehub/lobehub/pull/19572) | VERIFIED |
| **Milvus** (standalone compose + Helm) | **Rehosted a frozen MinIO**: `milvusdb/minio:RELEASE.2024-12-18T13-15-44Z` (multi-arch, synced from their Harbor). No migration | 2026-09-28 / Helm 2026-09-29 | [milvus#53889](https://github.com/milvus-io/milvus/pull/53889), `zilliztech/milvus-helm` #322 | VERIFIED |
| **Airbyte** (Helm) | **Rehosted**: `airbyte/minio:RELEASE.2023-11-20T22-40-07Z` | 2026-09-14 | airbyte-platform #19506, current `charts/v2/airbyte/values.yaml` | VERIFIED |
| **Budibase** | quay.io (2026-09-14), then **rehosted** `budibase/minio:final` | 2026-09-29 | Budibase commit `b62326091` | VERIFIED |
| **Dify** | **Unchanged**: `minio/minio:RELEASE.2023-03-20T20-16-18Z`, and only under the `milvus` profile (Docker Hub `minio/minio` now 404s) | none | current `docker/docker-compose.yaml` | VERIFIED |
| **Nextcloud server** (CI) | **Unchanged**: `bitnami/minio@sha256:50cec…` ("v2025.4.22"), pinned by digest. Whether it still pulls was not verified | none | `.github/workflows/object-storage.yml` | VERIFIED (file) |
| **Bitnami charts** | **Deprecated** the MinIO Helm chart ("MinIO is deprecated upstream"). The `bitnamilegacy/minio` image is frozen at 2025-08-19 | 2026-09-08 | [bitnami/charts#36605](https://github.com/bitnami/charts/pull/36605) | VERIFIED |
| Databricks `docker-spark-iceberg` | **Unchanged**: unpinned `minio/minio` + `minio/mc`, so presumably broken | none | current `docker-compose.yml` | VERIFIED (file) |
| Smaller but recent switches | SeaweedFS: NVIDIA retail-shopping-assistant blueprint (2026-10-02), classroomio (2026-10-08), basalt (2026-10-16), PostHog/ducklake CI (2026-10-23). RustFS: Akamai marketplace Milvus app (2026-10-05), try-playwright (2026-09-02) | as listed | [NVIDIA#271](https://github.com/NVIDIA-AI-Blueprints/retail-shopping-assistant/pull/271), [classroomio#1230](https://github.com/classroomio/classroomio/pull/1230), [basalt#471](https://github.com/basaltkit/basalt/pull/471), [ducklake#46](https://github.com/PostHog/ducklake/pull/46), [akamai#478](https://github.com/akamai-compute-marketplace/marketplace-apps/pull/478), [try-playwright#308](https://github.com/mxschmitt/try-playwright/pull/308) | VERIFIED |
| Immich, Appwrite, Outline, Mastodon, Penpot, Harbor | **No bundled MinIO** in the main compose file checked, so there was nothing to change. Mastodon's setup wizard lists "Minio" only as an S3 provider choice | n/a | current compose files / `lib/tasks/mastodon.rake` | VERIFIED (only the files checked; not exhaustive) |
| Cal.com, Zulip, PeerTube, Backstage, Dagster, Prefect, Airflow | No bundled MinIO found at the paths checked | n/a | n/a | not found |

**Aggregate signal: GitHub code-search hit counts (2026-10-31).** These are rough, and include
forks and duplicates:

| Image reference | Hits |
|---|---|
| `quay.io/minio/minio` | 17,888 (legacy) |
| `rustfs/rustfs` | 15,552 |
| `chrislusf/seaweedfs` | 12,528 |
| `cgr.dev/chainguard/minio` | 3,732 |
| `dxflrs/garage` | 3,712 |
| `pgsty/silo` | 3,696 |
| `pgsty/minio` | 2,420 |
| `versity/versitygw` | 2,108 |
| `adobe/s3mock` | 1,676 |

**Docker Hub pull counts.** These are lifetime totals, not a rate:

| Image | Pulls |
|---|---|
| `chrislusf/seaweedfs` | 37.2M |
| `adobe/s3mock` | 30.8M |
| `rustfs/rustfs` | 19.1M |
| `andrewgaul/s3proxy` | 10.3M |
| `dxflrs/garage` | 10.0M |
| `bitnamilegacy/minio` | 8.8M |
| `pgsty/minio` | 3.1M |
| `pgsty/silo` | 2.6M (renamed 2026-08-06) |
| `versity/versitygw` | 1.2M |

All counts are VERIFIED, via the API.

**Pattern worth copying.** Every project that left the MinIO *format* (PostHog, Onyx) did a
side-by-side copy through a temporary MinIO container onto a new volume. None migrated in place.
This matches §5.3 of the alternatives doc.

---

## (c) Local dev and test S3: what developers use now

| Choice | Consensus | Caveats (with source) |
|---|---|---|
| **SeaweedFS `weed mini`** | The most-recommended "real" store for dev and CI. An HN commenter says it fixed their flakiest CI tests, which were MinIO startup failures ([HN](https://news.ycombinator.com/item?id=47000041), REPORTED). Sentry runs `weed mini` in production compose (VERIFIED) | Without credentials it starts in an anonymous **"Allow All"** mode ([S3-Credentials wiki](https://github.com/seaweedfs/seaweedfs/wiki/S3-Credentials), VERIFIED). PostHog's compose comment warns against exactly this. Master **telemetry is on by default** (`-telemetry` defaults to true, posting to `telemetry.seaweedfs.com`, per `weed/command/master.go`, VERIFIED); Sentry passes `-master.telemetry=false`. `weed server` defaults `-dir` to **/tmp**, which wiped Sentry users' data on restart ([self-hosted#3990](https://github.com/getsentry/self-hosted/issues/3990), VERIFIED, fixed in 25.10.0) |
| **Patched MinIO fork** (Silo / Chainguard) | The zero-code-change answer for teams whose code or tests assume MinIO (see table (b)) | Chainguard's free tier is `latest`/`latest-dev` only, runs nonroot UID 65532 and has no curl. Langfuse-derived PRs switch the healthcheck to `mc ready local` and add `user: root` for old volumes ([fazer-ai/agents#1035](https://github.com/fazer-ai/agents/pull/1035), REPORTED) |
| **RustFS** | "Dead simple to set up" and "close to a MinIO rewrite" (HN, REPORTED). rmoff rated it easy but young ([rmoff, 2026-01](https://rmoff.net/2026/01/14/alternatives-to-minio-for-single-node-local-s3/), REPORTED) | **42 GitHub security advisories published in 2026** (4 critical, 17 high, 17 medium, 4 low; plus 1 in 2025), VERIFIED via the API. The CLA grants RustFS the right to relicense contributions "under any license, including proprietary" ([CLA.md](https://github.com/rustfs/rustfs/blob/main/CLA.md), VERIFIED), and there is a `rustfs/src/license.rs` module. HN users read this as rug-pull risk (REPORTED). Performance complaints on 512 KB–1 MB objects ([Habr](https://habr.com/ru/post/1007060), REPORTED). 1.0 GA was only in Sep 2026 |
| **Garage** | Liked for homelab and multi-site use. One HN user calls it best for dev | rmoff: hard to set up for local use (needs an init container plus TOML), "not just a drop-in" (REPORTED). No object tagging or bucket policy (alternatives doc). CORS **is** supported per the maintainer, contradicting an earlier commenter ([HN](https://news.ycombinator.com/item?id=47000041), REPORTED) |
| **versitygw** | "Simplest S3 on top of a local folder" (HN, REPORTED) | Reported sync-IO pain on ZFS/HDD ([Ask HN](https://news.ycombinator.com/item?id=47977081), REPORTED). 8 advisories in 2025–26, VERIFIED |
| **LocalStack** | Formerly the default AWS emulator | Since **2026-03-23** `localstack/localstack` is a unified image that **requires an auth token**. The GitHub repo is archived (VERIFIED: `archived=true`, last push 2026-03-23). There is a `community-archive` tag ([LocalStack blog](https://blog.localstack.cloud/localstack-for-aws-release-2026-03-0/), [single-image post](https://blog.localstack.cloud/localstack-single-image-next-steps/), REPORTED via search) |
| **Adobe S3Mock / moto server / S3Proxy** | Test doubles. Actively maintained (pushed 2026-10-30, VERIFIED) | Not durable stores, for unit/integration tests only. S3Proxy depends on jclouds, which was retired to the Apache Attic (rmoff, REPORTED) |
| **Zenko CloudServer** | rmoff: drops in easily | amd64-only on ghcr (alternatives doc) |

**Forum caveat.** No Reddit threads surfaced through search, so the Reddit consensus is **not
found**. The two HN threads (500 points / 368 comments, Feb 2026; and an "Ask HN" from May 2026)
are the best primary community sources. Blog roundups (Pinggy, SSD Nodes, GigeNET, Storm
Developments, rilavek) broadly say:
- RustFS for the easiest MinIO-like drop-in;
- SeaweedFS for maturity;
- Garage for small multi-site clusters.

Some of those roundups are vendor-authored; Storm Developments sells hosted Garage.

---

## (d) Forks and maintainers

| Fork | Maintainer | Signals | Assessment |
|---|---|---|---|
| **PGSTY Silo** (`pgsty/silo`, ex-`pgsty/minio`) | Vonng / Pigsty. **562 of ~588 commits since the fork (2025-10-25) are Vonng's**, VERIFIED | 4,421 stars, 261 forks. 2.6M + 3.1M Hub pulls. 12 releases in 2026, roughly monthly. 0 GHSAs in its own repo (it fixes upstream ones in release notes). Adopted by Loki (compose and Helm), Mimir, Argo, Plane, RAGFlow, Coolify and testcontainers-java; GitLab QA MR pending. The author claims "30+ projects" plus nixpkgs and DaoCloud ([blog, 2026-08-06](https://vonng.com/en/db/long-live-silo/), REPORTED) | **The de facto community fork.** The author says plainly it is "what one person maintaining a mid-sized infrastructure project looks like". The manifesto promises a release every 1–2 months, and public notice plus archiving if maintenance stops for more than six months ([README](https://github.com/pgsty/silo)) |
| **Chainguard MinIO** (`chainguard-forks/minio` → Wolfi `minio` → `cgr.dev/chainguard/minio`) | Chainguard staff (tdunlap607, 8none1, sergiodj). SECURITY.md points at Chainguard | The fork carries fixes for **CVE-2026-41145, -40344, -34204, -42600, -33322, -33814** and SigV4 unsigned-header rejection (2026-09-22). Latest tag `RELEASE.2026-10-23T20-06-51Z`, which is what Wolfi `minio` 0.20261023.200651 builds (epoch bump for CVE-2026-78661). Adopted by Langfuse, Supabase self-host and Mattermost | **New finding: a company-staffed, security-only fork.** It resolves the alternatives doc's "Chainguard version unknown / CVE fixes unverified" for the *source*. That the free `latest` digest's SBOM matches the tag is not verified. Version tags are paid; free users pin by digest, and digest retention is unknown. Because the source tags are public AGPL, **OT could build it itself** |
| `pgsty/minio` (old name) | same as Silo | frozen 2026-08-04 | Do not use |
| `coollabsio/minio` | Coolify | Rebuild of upstream `RELEASE.2025-10-15`, last push 2025-10-29. 165k pulls | Frozen; missing the 2026 fixes |
| `bitnamilegacy/minio` | Bitnami (legacy) | frozen 2025-08-19. 8.8M pulls | Frozen; Bitnami chart deprecated |
| Project rehosts (`milvusdb/minio`, `airbyte/minio`, `budibase/minio`, `onyxdotapp/minio`) | each project | 2023–2025 MinIO builds | These are survival copies, not maintained forks. They carry the HIGH CVEs |
| OpenMaxIO | community | 1,970 stars. **Console only**, last push 2025-06-24 | Not a server |
| `ossfork/pgsty-minio`, `arrrrny/minio` | n/a | mirrors of Silo | Not independent (REPORTED) |

No Docker Official Image, CNCF project or Bitnami asset points at a fork. Among CNCF and Grafana
projects, Argo Workflows (CNCF) and Loki/Mimir/Thanos chose Silo or SeaweedFS as listed above.

---

## (e) SeaweedFS as a single-node default for OpenTranscribe

**Adoption.** SeaweedFS has the most-adopted non-MinIO footprint among serious self-hosted apps:
Kubeflow, Sentry, PostHog, Onyx and Thanos e2e. It has 35.8k stars, 37.2M pulls and roughly weekly
releases (4.44 on 2026-08-22 through 4.51 on 2026-10-25). All VERIFIED.

**Feature maturity against what OT uses.** All of the following are VERIFIED from the
[Amazon-S3-API wiki](https://github.com/seaweedfs/seaweedfs/wiki/Amazon-S3-API) and its subpages,
as of 2026-10-31:
- **Presigned URLs**, PostObject, range GET, CopyObject, DeleteObjects.
- **The full multipart set**: Create, UploadPart, UploadPartCopy, Complete, Abort, ListMultipartUploads, ListParts.
- **Object tagging**: get, put and delete.
- **Bucket policy**: get, put and delete.
- **Object lock, retention and legal hold.**
- Versioning, CORS, and SSE-S3/SSE-KMS/SSE-C.
- **Lifecycle**: `Expiration.Days/Date`, `Filter.Prefix`, `Filter.Tag` and
  `AbortIncompleteMultipartUpload` are supported. Transition is rejected.
- **Policy condition keys** include `s3:ExistingObjectTag/<key>`
  ([S3-Policy-Conditions](https://github.com/seaweedfs/seaweedfs/wiki/S3-Policy-Conditions)).
  This was an unverified item in the alternatives doc; it is now documented, but still untested.
- **IAM API** includes `CreateUser`, `CreateAccessKey`, `PutUserPolicy` (inline) and
  `CreateServiceAccount` ([Amazon-IAM-API](https://github.com/seaweedfs/seaweedfs/wiki/Amazon-IAM-API)),
  so the #907 restricted presign identity looks portable.
- `-s3.externalUrl` exists for reverse proxies. That is relevant to OT's `/s3` host rewrite
  ([S3-Nginx-Proxy](https://github.com/seaweedfs/seaweedfs/wiki/S3-Nginx-Proxy)).
- SeaweedFS also implements `x-amz-meta-snowball-auto-extract`. That is the MinIO feature behind
  CVE-2026-40344, so its auth path should be checked before adoption (not verified).

**Single-node mode.** The project positions `weed mini` as appropriate for "single-node
production setups (S3 gateway, file storage, presigned URLs)". Its defaults "may evolve between
releases", so pin the version and pass the flags explicitly
([Quick-Start-with-weed-mini](https://github.com/seaweedfs/seaweedfs/wiki/Quick-Start-with-weed-mini),
VERIFIED).

The reference compose is Sentry's (VERIFIED):

```yaml
command: >-
  mini -dir=/data -filer.defaultReplicaPlacement=000 -master.telemetry=false
  -volume.dir.idx=/data/idx -volume.index=leveldbLarge -volume.preStopSeconds=8
  -volume.readMode=redirect -ip.bind=0.0.0.0 -webdav=false
```

It also sets credentials through the env `AWS_ACCESS_KEY_ID`/`AWS_SECRET_ACCESS_KEY`, and its
healthcheck probes `:9340/healthz`, `:9333/cluster/healthz` and `:8333/healthz` with `wget`.

**Memory.** Not measured here.
- Wiki (VERIFIED): the in-memory volume index costs about 20 B per file, and the leveldb index
  variants use 4–12 MB in total ([Optimization](https://github.com/seaweedfs/seaweedfs/wiki/Optimization)).
- REPORTED: about 60–100 MB idle for `weed server -s3`
  ([SitePoint](https://www.sitepoint.com/local-s3-storage-without-minio-seaweedfs-garage-docker-compose/)).
  The column attribution there was inferred.
- REPORTED: about 804 MB idle for a 3-container deployment
  ([gist](https://gist.github.com/komsit37/7029089c05b741931dd21ac49687dd4b)).

**Concerns for OT.**
1. **Bus factor.** chrislusf wrote 1,480 of the 1,983 non-bot commits since 2026-05-01 (about
   75%, VERIFIED). That is better than Silo's ~96%, but still one person. A commercial SeaweedFS
   enterprise offering exists (rmoff, REPORTED).
2. **Advisory volume.** 25 GHSAs published in 2026 (9 critical, 12 high), clustered in Jun–Sep
   2026 (VERIFIED). Many target internal gRPC, volume, filer, SFTP or Raft surfaces. In OT's
   compose, **publish only the S3 port** and keep 9333, 9340, 8888 and 18xxx off the network.
3. **Unsafe defaults.** Anonymous Allow-All without credentials, telemetry on, and `/tmp` data
   dirs under `weed server` (see (c)).
4. **Space reclaim after deletes and lifecycle.** Sentry users saw terabyte-scale growth that
   cleanup did not reclaim ([self-hosted#4106](https://github.com/getsentry/self-hosted/issues/4106),
   VERIFIED reports; root cause not established). There was also a `setlifecycle` hang on upgrade
   ([#4257](https://github.com/getsentry/self-hosted/issues/4257)). OT deletes media for
   takedowns, so vacuum behaviour must be rehearsed.
5. **No in-place migration from MinIO** (PostHog, VERIFIED). A copy is needed, as in alternatives
   doc §5.3.

**Verdict.** It remains the right long-term candidate. Adopting it means a hardened compose
profile (credentials, telemetry off, explicit dirs, only the S3 port exposed), a #907 port onto
its IAM API, and a delete/vacuum rehearsal. It is not a v0.6.0 change.

---

## (f) Could not verify

- Merge status and date of GitLab GDK changes; and the Loki Helm chart's Silo switch date (the
  file content is verified).
- Whether `cgr.dev/chainguard/minio:latest`'s current digest SBOM equals `RELEASE.2026-10-23T20-06-51Z`,
  and how long Chainguard keeps old free digests pullable.
- Whether `bitnami/minio@sha256:50cec…` (Nextcloud CI) still pulls.
- Reddit consensus. No Reddit threads were returned by search.
- Idle RSS for `weed mini`, Silo or Chainguard. None were measured, and the third-party numbers
  conflict.
- SeaweedFS `s3:ExistingObjectTag` Deny on GetObject, `response-content-disposition` on presigned
  GET, and Snowball auto-extract auth. All are documented or present, none tested.
- The full list of Silo's "30+ adopters". Only those in table (b) were confirmed at source.
- The exact RustFS fix version for each 2026 advisory. Ranges came from third-party CVE pages.
- LocalStack token details were read from search summaries of its blog, not from the blog page itself.
- Whether Supabase's hosted `supabase start` CLI stack contains any MinIO. The CLI repo search was
  inconclusive.
