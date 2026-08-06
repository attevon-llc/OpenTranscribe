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

# The offset is remembered per deployment, so a later re-up keeps the same
# ports. Pass --port-offset 0 to move it back to the standard ports.
./opentr.sh start dev --fresh test1

# Upload a couple of small sample media files once the stack is healthy.
./opentr.sh start dev --fresh test1 --seed-benchmark

# The --with-* test overlays come along, fully isolated: the LLDAP container is
# otfresh-test1-lldap on 127.0.0.1:3990, and this stack's backend reaches it —
# not the main stack's.
./opentr.sh start dev --fresh test1 --port-offset 100 --with-ldap-test
```

### Managing fresh deployments

```bash
./opentr.sh stop   --fresh test1   # stop containers (named volumes kept)
./opentr.sh status --fresh test1   # show this deployment's containers
./opentr.sh fresh-list             # list all fresh deployments + their volumes
./opentr.sh fresh-destroy test1    # remove containers AND volumes (y/N confirm)
```

`fresh-destroy` is the **only** destructive fresh operation. It always shows
exactly which containers, volumes, and generated files it will remove, and
prompts for confirmation (defaults to **No**). It never touches any bind path or
any other stack.

### How container-name collisions are solved

The base `docker-compose.yml` hard-codes `container_name: opentranscribe-*` on
every service. A compose project name alone does **not** override those (Docker
container names are global), and compose cannot *unset* `container_name` via an
overlay. So fresh mode generates a tiny overlay at `.fresh/<name>.yml` that
explicitly re-pins every service to `otfresh-<name>-*`. It is gitignored and
regenerated on every start.

The aux test overlays that hard-code a container name (`lldap`, `smb-test`,
`prometheus`, `grafana`) are added to that same generated overlay — but only
when the matching `--with-*` flag is passed, since an overlay entry for a
service no compose file defines makes `up` fail.

Renaming a container does **not** change how other containers reach it: compose
registers each service's *service name* as a network alias, and everything
in-network is addressed that way (`backend:8080`, `prometheus:9090`,
`postgres:5432`, `smb-test:445`). The one exception is LLDAP, which is
documented and configured as `ldap://lldap-test` — the container name. The
overlay therefore pins `lldap-test` as an explicit network alias, so that name
keeps resolving inside every project, fresh or not.

### How `--port-offset` moves the ports

Every published port in `docker-compose.yml` / `docker-compose.override.yml` is
written as `"${SOME_PORT:-<default>}:<container port>"`. `--port-offset N`
**exports those variables** with `N` added, so compose substitutes the moved
port into the mapping that is already there:

| Variable | Default | Service |
|---|---|---|
| `FRONTEND_PORT` | 5173 | frontend (dev override) |
| `BACKEND_PORT` | 5174 | backend |
| `FLOWER_PORT` | 5175 | flower |
| `POSTGRES_PORT` | 5176 | postgres |
| `REDIS_PORT` | 5177 | redis |
| `MINIO_PORT` | 5178 | minio API |
| `MINIO_CONSOLE_PORT` | 5179 | minio console |
| `OPENSEARCH_PORT` | 5180 | opensearch API |
| `OPENSEARCH_ADMIN_PORT` | 5181 | opensearch admin |
| `DOCS_PORT` | 5183 | docs (dev override) |

If `.env` already sets one of these, the offset is applied to *that* value, not
to the default.

Each `--with-*` test overlay adds its own ports to the same treatment when the
flag is passed:

| Flag | Variable | Default | Service |
|---|---|---|---|
| `--with-ldap-test` | `LDAP_TEST_PORT` | 3890 | LLDAP LDAP |
| `--with-ldap-test` | `LDAP_TEST_UI_PORT` | 17170 | LLDAP web UI |
| `--with-smb-test` | `SMB_TEST_PORT` | 4450 | Samba share |
| `--with-monitoring` | `GRAFANA_PORT` | 5185 | Grafana |
| `--with-monitoring` | `PROMETHEUS_PORT` | 5186 | Prometheus |
| `--with-keycloak-test` | `KEYCLOAK_PORT` | 8180 | Keycloak |
| `--with-keycloak-test` | `STEP_CA_PORT` | 9000 | Step CA |

:::note `LDAP_TEST_PORT`, not `LDAP_PORT`
`LDAP_PORT` is the **application's** LDAP client port (`.env` ships
`LDAP_PORT=636`). Offsetting that would silently repoint the app's LDAP config,
so the test container's host ports get their own `LDAP_TEST_*` names.
:::

The offset is recorded in `.fresh/<name>.offset` so `status`, `fresh-list`, and a
later re-up without the flag all address the same ports. `--port-offset 0`
clears it. The `--with-*` overlays a deployment was started with are recorded in
`.fresh/<name>.aux` for the same reason: `stop`, `status`, and `fresh-destroy`
have to load the same compose chain the deployment was created with, or the
generated overlay would re-pin a `container_name` for a service compose can no
longer see.

:::info Why not a generated ports overlay?
Fresh mode used to write a second compose file that redeclared each service's
`ports:` list. Compose **appends** port lists when merging files instead of
replacing them, so the base mapping stayed published alongside the offset one —
the "isolated" stack still bound the main stack's ports (issue #343). Setting
the variables the base file already reads leaves exactly one mapping per port,
and keeps the `127.0.0.1:`-only binding the base file puts on the infra ports.
:::

Before starting, `--port-offset` checks every resolved port and refuses to start
if something else already holds one, rather than failing halfway through
`compose up` with a bind error.

:::warning Host-bind overlays are still shared
`--with-watch` and `--with-backup` mount **live host directories**
(`WATCH_HOST_PATH`, `BACKUP_HOST_PATH`, `BACKUP_MIRROR_HOST_PATH`) into the
stack. A fresh deployment reads and writes the same folders as the main stack —
the isolation `--fresh` gives you is on containers, ports, and named volumes,
not on a host path you explicitly asked to bind. `opentr.sh` warns when you
combine them; point the variables at a scratch directory first if that matters.

The `--with-ldap-test` / `--with-smb-test` / `--with-monitoring` /
`--with-keycloak-test` overlays *are* isolated (issue #347): container names,
host ports, and named volumes all move with the deployment.
:::

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
