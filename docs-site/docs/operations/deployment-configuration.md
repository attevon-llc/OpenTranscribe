---
sidebar_position: 2
title: Deployment Configuration
description: Every deployment type and its launch command, the first-init healthcheck model, the cross-worker scratch contract, GPU modes, security posture, and the storage overlay
---

# Deployment Configuration

OpenTranscribe ships as a base `docker-compose.yml` plus a set of **overlay**
files that are layered on for each deployment scenario. The `./opentr.sh` script
composes the correct overlay set for you — **always launch the stack through it**
rather than bare `docker compose`, so containers get the right database, storage,
network, and environment.

:::tip[Use `./opentr.sh`]
`./opentr.sh start dev` (and the flags below) selects the correct `-f` overlay
chain. Bare `docker compose up` skips the overlays and can attach to a
differently-configured stack — symptoms include schema errors, wrong storage, or
workers that silently re-download every file. See
[Fresh & Isolated Deployments](./fresh-deployments.md) for safe experimentation.
:::

## Deployment types and launch commands

| Deployment | Command | Notes |
|---|---|---|
| **Dev (default)** | `./opentr.sh start dev` | Vite hot-reload, relaxed auth limits, auto-loads `docker-compose.override.yml`. |
| **Production** | `./opentr.sh start prod --build` | Pre-built/local images, nginx, strict auth. |
| **CPU-only** | `./opentr.sh start dev --cpu` | Local transcription on CPU; skips the GPU overlay. |
| **Lite (cloud ASR)** | `./opentr.sh start dev --lite` | No GPU; transcription via a configured cloud ASR provider. Speaker embeddings (v4/256-d) come from the diar-native CPU-EP sidecar, not an in-process PyAnnote model — the lite image ships neither `pyannote.audio` nor `torchaudio` (issue #660). Add `--with-diar-native` and export `DIAR_NATIVE_MODELS_DIR` at a weights export produced elsewhere (the lite image has no Python exporter toolchain to provision its own); v3 (512-d) speaker data is unserviceable under lite until migrated to v4. |
| **GPU scale (dual-GPU)** | `./opentr.sh start dev --gpu-scale` | N parallel workers on `GPU_SCALE_DEVICE_ID`; keeps the default worker too when `GPU_SCALE_DEFAULT_WORKER=1`. |
| **GPU split** | `./opentr.sh start dev --with-gpu-split` | Transcription and diarization on **separate** GPUs. Needs `ENGINE_GPU_SPLIT=true`. |
| **Native diarization sidecar** | `./opentr.sh start dev --with-diar-native` | Runs `diar-server` (Rust/ONNX) alongside `celery-worker` instead of in-process PyAnnote. **Windows installer excluded** — see `windows-installer/INSTALL-WINDOWS.md`. |
| **NAS / NVMe storage** | `./opentr.sh start dev --nas` | Bind-mounts custom host paths for media/DB/search. Auto-detected from `.env`; `--no-nas` suppresses it. |
| **Fresh / isolated** | `./opentr.sh start dev --fresh <name>` | Throwaway stack, own project + volumes, NAS overlay never loaded. See [Fresh Deployments](./fresh-deployments.md). |
| **Monitoring** | `./opentr.sh start dev --with-monitoring` | Prometheus (:5186) + Grafana (:5185). See [Monitoring](./monitoring.md). |
| **Watch sources** | `./opentr.sh start dev --with-watch` | Mounts `WATCH_HOST_PATH` for auto-import. |
| **In-app backups** | `./opentr.sh start dev --with-backup` | Mounts `BACKUP_HOST_PATH` for scheduled backups. See [Backup & Restore](./backup-restore.md). On a production install, set `BACKUP_OVERLAY_ENABLED=true` in `.env` instead (`./opentranscribe.sh` has no `--with-*` flags) — commented out by default since it also recreates the OpenSearch container. |
| **LDAP test IdP** | `./opentr.sh start dev --with-ldap-test` | lldap at `localhost:3890`, UI `:17170`. |
| **Keycloak test IdP** | `./opentr.sh start dev --with-keycloak-test` | Keycloak at `localhost:8180`. |
| **SMB test share** | `./opentr.sh start dev --with-smb-test` | Samba share for watch-source testing. |
| **PKI / mTLS** | `./opentr.sh start prod --build --with-pki` | mTLS at `https://localhost:5182`. **Production mode only** (Vite can't do mTLS). |
| **Offline / air-gapped** | See `scripts/install-offline-package.sh` | Pre-downloaded models, `HF_HUB_OFFLINE=1`, no network calls. |
| **Benchmark** | `./opentr.sh bench …` | Isolated `otbench-*` stack for performance measurement. |
| **AWS (managed OpenSearch + S3)** | `./opentr.sh start prod --build` with `.env` set to [the AWS profile](../configuration/environment-variables.md#the-aws-profile) | No dedicated overlay/flag — SigV4 auth, a managed embedding model, native S3, and (on a multi-node domain) `OPENSEARCH_CHUNKS_INDEX_REPLICAS>0` are all plain env vars the existing seams already branch on. |

Flags combine where they make sense (e.g. `--gpu-scale --nas`,
`--with-monitoring --with-watch`). Mixing dev and prod overlays requires explicit
flags because the dev override is **not** auto-loaded once you pass other
overlays.

:::note[PKI in development]
The dev `--with-pki` flow uses `docker-compose.pki-dev.yml`, which only overrides
`frontend` + `backend` — every other service comes from the dev override, so the
override **must** be in the chain (`./opentr.sh` handles this). Because the dev
override already publishes Vite on `:5173` and the docs site on `:5183`, the PKI
nginx frontend's plain-HTTP port is published on a distinct host port
(`PKI_HTTP_PORT`, default `5187`); the mTLS entrypoint is `PKI_HTTPS_PORT`
(default `8443`).
:::

## First-init healthcheck model (why it matters)

On a **fresh start against a large or bind-mounted data directory**, the
datastores need time to initialize — PostgreSQL creates the cluster and WAL,
MinIO reconciles buckets/IAM (the real media volume can be hundreds of GB), and
OpenSearch boots the JVM and recovers shards. The application tier
(`backend`, workers) starts only after these are healthy, via Docker Compose
`depends_on: { condition: service_healthy }`.

The failure this prevents: if a datastore's first init takes longer than the
healthcheck's `retries × interval` window, Compose marks it **unhealthy** and
**aborts every service that depends on it** — leaving containers stuck in the
`Created` state, or the backend running migrations against a half-built schema
("relation does not exist").

The base compose therefore gives each datastore a healthcheck **`start_period`**
(a grace window during which failing probes don't count against the retry
budget):

| Service | `start_period` | Reason |
|---|---|---|
| `postgres` | 60 s | Cluster create + WAL setup on a fresh bind mount. |
| `minio` | 60 s | First-boot bucket/IAM reconciliation on a large data dir. |
| `opensearch` | 60 s | JVM boot + shard recovery on a large existing index. |
| GPU / CPU / embedding / model workers | 120 s | Cold model preload + first-run HuggingFace download. |

`redis` uses a tight healthcheck (5 s timeout, 10 retries). Every worker —
including `celery-nlp-worker` — waits on `backend: service_healthy`, so no worker
can race the schema before migrations have applied.

`./opentr.sh start` and `reset` launch with **`up -d --wait --wait-timeout 700`**:
the command blocks until every service is healthy (the timeout covers the
backend's start window). A container that is created but never becomes healthy now
surfaces as a non-zero exit with a service-status table and recent logs, instead
of an optimistic "starting up" message.

## The `pipeline_scratch` cross-worker handoff

Transcription is a two-stage pipeline: a CPU worker preprocesses the source media
into a 16 kHz WAV, and a GPU worker consumes that WAV for transcription +
diarization. To avoid re-downloading the (much larger) source from MinIO on the
GPU side, same-host workers hand the WAV off through a **shared named volume**,
`pipeline_scratch`, mounted at `/scratch/opentranscribe`:

- The CPU worker stages the preprocessed WAV into the scratch volume (atomic
  rename + hard-link).
- The GPU worker reads it directly from the same volume.
- A MinIO fallback covers the multi-host case (different physical hosts can't
  share the volume).

**The contract:** every worker that participates in transcription **must mount
`pipeline_scratch:/scratch/opentranscribe`**. If a worker is missing the mount it
can't see the staged WAV and silently falls back to re-downloading each file from
MinIO — correct, but much slower. This mount is now present on the default GPU
worker, the scaled GPU worker, and both GPU-split workers across the dev, prod,
and offline overlays.

:::warning[Scratch volume permissions]
The `pipeline_scratch` volume is root-owned when first created, but workers run as
UID 1000. `./opentr.sh` chowns it to `1000:999` (the container `appuser`) on startup; if you create the
stack by other means, the handoff will fall back to MinIO until the volume is
writable by the worker user.
:::

## GPU modes

OpenTranscribe supports three GPU topologies. All device IDs below are **host**
GPU indices (as seen by `nvidia-smi`).

### Single GPU (default)

One GPU runs both transcription and diarization. Set `GPU_DEVICE_ID` to the card
to use:

```bash
GPU_DEVICE_ID=0
./opentr.sh start dev
```

### Dual GPU (scale + keep default worker)

Run N parallel workers on a dedicated GPU **and** keep the default worker on its
own GPU, so **both cards do transcription**:

```bash
GPU_SCALE_ENABLED=true
GPU_DEVICE_ID=0            # default worker runs here
GPU_SCALE_DEVICE_ID=2     # N parallel scaled workers run here
GPU_SCALE_WORKERS=4
GPU_SCALE_DEFAULT_WORKER=1 # 1 = keep the default worker too (dual-GPU); 0 = scaled only
./opentr.sh start dev --gpu-scale
```

`GPU_SCALE_DEFAULT_WORKER=1` is the dual-GPU toggle: it keeps the default worker
on `GPU_DEVICE_ID` alongside the scaled workers on `GPU_SCALE_DEVICE_ID`. Set it
to `0` to dedicate the default GPU to other work.

### Split GPU (transcription on one card, diarization on another)

Run the WhisperX transcription stage and the PyAnnote diarization stage on
**separate** GPUs for higher throughput on a 2+ GPU host:

```bash
ENGINE_GPU_SPLIT=true
GPU_TRANSCRIBE_DEVICE_ID=0   # host GPU for the gpu-transcribe worker
GPU_DIARIZE_DEVICE_ID=1      # host GPU for the gpu-diarize worker
./opentr.sh start dev --with-gpu-split
```

This loads `docker-compose.gpu-split.yml`, which activates the `gpu-transcribe` /
`gpu-diarize` worker services (defined in the base compose under the `gpu-split`
profile) and grants each a **dedicated GPU reservation**. The two device IDs must
be **different** for the split to help — if they're equal, both stages share one
card with no benefit.

#### Device reservation → `cuda:0` mapping

When a container reserves exactly one GPU via Docker's `device_ids`, Docker
**remaps that reserved card to index 0 inside the container**. So although the
host might assign GPU 1 to the diarize worker, inside that container the card is
`cuda:0`. For this reason both split workers (and the scaled workers) set
`CUDA_VISIBLE_DEVICES=0` — the in-container index — rather than the host index.
The host-to-container assignment is controlled entirely by the
`device_ids: ["${GPU_TRANSCRIBE_DEVICE_ID}"]` / `["${GPU_DIARIZE_DEVICE_ID}"]`
reservations in `docker-compose.gpu-split.yml`.

## Security posture

The base and overlay compose files apply a defense-in-depth baseline:

- **Loopback-only infrastructure ports**: `postgres`, `redis`, `opensearch`
  (and its admin port), `minio` (API + console), and `flower` publish their host
  ports as `127.0.0.1:<port>:<container>`, **not** `0.0.0.0`. These services are
  reached internally over the compose network (`postgres:5432`, `minio:9000`,
  etc.); the host ports exist only for local tooling and tests and are not
  exposed to the LAN. The application frontend/nginx ports are unchanged.
- **`no-new-privileges`** is set on the core services and on every auxiliary
  container (nginx, keycloak, step-ca, lldap, samba), preventing setuid
  privilege escalation inside the containers.
- **Generated secrets**: the installers (`setup-opentranscribe.sh`,
  `install-offline-package.sh`) generate strong random values for all
  credentials — including `OPENSEARCH_ADMIN_PASSWORD` (complexity-compliant for
  the OpenSearch security plugin) and the MinIO at-rest encryption key — using
  `openssl` / `python3` / `/dev/urandom` (never a predictable timestamp). API
  keys are read with `read -s` (no terminal echo), and the generated `.env` is
  `chmod 600` (owner-only).
- **`OPENSEARCH_ADMIN_PASSWORD`** is only consumed when the OpenSearch security
  plugin is enabled (`OPENSEARCH_SECURITY_ENABLED=true` /
  `OPENSEARCH_DISABLE_SECURITY=false`); leave it blank when security is disabled
  (the dev default).

See [Security Hardening](./security-hardening.md) for the full production
checklist.

## Storage overlay (NAS / NVMe)

By default all primary data lives in **Docker named volumes**. The optional
NAS/NVMe overlay (`docker-compose.nas.yml`) instead **bind-mounts** custom host
paths for media (MinIO), the database (PostgreSQL), and the search index
(OpenSearch), configured in `.env`:

```bash
MINIO_NAS_PATH=/mnt/nas/opentranscribe/media
POSTGRES_DATA_PATH=/mnt/nvme/opentranscribe/postgres
OPENSEARCH_DATA_PATH=/mnt/nvme/opentranscribe/opensearch
```

The overlay is **auto-loaded** when any of those paths is set (with a banner);
`--no-nas` suppresses it (use named volumes; live bind data untouched) and
`--nas` opts in explicitly.

:::info[Schema is built by Alembic, not `init_db.sql`]
The NAS overlay **no longer mounts the legacy `database/init_db.sql`**. The schema
is built by Alembic/Python on backend startup (migrations run automatically), so
the init script was redundant — and on a large bind mount it slowed the first
boot enough to trigger the datastore healthcheck race described above.
`database/init_db.sql` remains in the repo as a legacy reference only.
:::

:::warning[Protect live data]
Every NAS-overlay start writes a `.opentranscribe-live-data` marker into each
bind-mounted directory. Run `./opentr.sh data-paths` to see exactly which host
paths hold live data **before** deleting or cleaning up anything. Use
[`--fresh`](./fresh-deployments.md) for any experimental stack so it can never
touch this data.
:::
