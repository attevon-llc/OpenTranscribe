---
sidebar_position: 9
title: Fresh & Isolated Deployments
description: Spin up throwaway stacks that can never touch your live data, and find out exactly where your data lives
---

# Fresh & Isolated Deployments

OpenTranscribe's primary data lives in either **Docker named volumes** (default)
or **bind-mounted host directories** (the NAS/NVMe overlay, when configured in
`.env`). Experimental work, demos, and one-off tests should **never** run against
that live data. The `--fresh` mode gives you a fully isolated stack that runs in
its own compose project with its own named volumes, with the NAS/bind overlay
guaranteed off.

:::warning Protect your live data
Before deleting or "cleaning up" any directory, run `./opentr.sh data-paths` to
see which paths hold live, bind-mounted data. Those directories carry a
`.opentranscribe-live-data` marker file when the NAS overlay is active.
:::

## Fresh deployments

A fresh deployment uses the compose project `otfresh-<name>`. Containers are
named `otfresh-<name>-*` and volumes are `otfresh-<name>_*` — zero collision
with the main `opentranscribe-*` stack. **The NAS overlay is never loaded in
fresh mode, even when `.env` defines storage paths.**

```bash
# Start an isolated stack on the standard dev ports (5173-5181).
# Refuses to start if the main stack already holds those ports.
./opentr.sh start dev --fresh test1

# Run side-by-side with the main stack by offsetting every published port.
# With +100: backend on :5274, frontend on :5273, OpenSearch on :5280, etc.
./opentr.sh start dev --fresh test1 --port-offset 100

# Upload a couple of small sample media files once the stack is healthy.
./opentr.sh start dev --fresh test1 --seed-benchmark
```

### Managing fresh deployments

```bash
./opentr.sh stop   --fresh test1   # stop containers (named volumes kept)
./opentr.sh status --fresh test1   # show this deployment's containers
./opentr.sh fresh-list             # list all fresh deployments + their volumes
./opentr.sh fresh-destroy test1    # remove containers AND volumes (y/N confirm)
```

`fresh-destroy` is the **only** destructive fresh operation. It always shows
exactly which containers, volumes, and generated overlay files it will remove,
and prompts for confirmation (defaults to **No**). It never touches any bind
path or any other stack.

### How container-name collisions are solved

The base `docker-compose.yml` hard-codes `container_name: opentranscribe-*` on
every service. A compose project name alone does **not** override those (Docker
container names are global), and compose cannot *unset* `container_name` via an
overlay. So fresh mode generates a tiny overlay at `.fresh/<name>.yml` that
explicitly re-pins every service to `otfresh-<name>-*`. With `--port-offset`, a
second generated overlay `.fresh/<name>-ports.yml` republishes each port at its
default + offset. Both files are gitignored and regenerated on demand.

## NAS overlay directives (non-fresh)

For the normal (non-fresh) `start`, the NAS/bind overlay still auto-loads when
`MINIO_NAS_PATH`, `POSTGRES_DATA_PATH`, or `OPENSEARCH_DATA_PATH` are set in
`.env` — but it now announces itself:

```
💾 NAS overlay AUTO-LOADED from .env (storage at MinIO=..., PG=..., OS=...). Use --no-nas to skip.
```

| Flag        | Effect                                                        |
|-------------|---------------------------------------------------------------|
| *(none)*    | Auto-load the NAS overlay if storage paths are set in `.env`. |
| `--nas`     | Explicitly load the NAS overlay.                              |
| `--no-nas`  | Suppress the NAS overlay; use Docker named volumes instead.   |

When the NAS overlay is active on a non-fresh start, a best-effort
`.opentranscribe-live-data` marker is written into each bind directory:

```
LIVE DATA — bind-mounted into the OpenTranscribe stack. DO NOT delete or
'clean up'. Managed by opentr.sh. See ./opentr.sh data-paths.
```

## Inspecting data locations

```bash
./opentr.sh data-paths
```

Prints the resolved live data locations — the MinIO / PostgreSQL / OpenSearch
bind paths (and whether the NAS overlay is active), or the named-volume names
used otherwise — plus any fresh deployments. This is the canonical thing to
check, by both humans and automated cleanup agents, before deleting anything.

## Dry run

Append `--dry-run` to any `start` to print the exact compose file list and the
`docker compose ... up` command that would run, **without launching anything**:

```bash
./opentr.sh start dev --fresh test1 --dry-run
```
