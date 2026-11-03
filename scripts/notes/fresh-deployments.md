# scripts — fresh deployments (`opentr.sh --fresh`) mechanics

Moved intact from `scripts/CLAUDE.md`. The repo-wide rule (use `--fresh` for anything throwaway) stays in the root `CLAUDE.md`; operator view: `docs-site/docs/operations/fresh-deployments.md`.

## Fresh deployments (`opentr.sh --fresh`, state in `.fresh/`)

Root `CLAUDE.md` points here for the mechanics. `.fresh/` is gitignored and fully regenerated.

**Starting `--fresh` from a git worktree (issue #961a):** `.env` is gitignored, so a worktree
does not get one. `opentr.sh`'s prologue auto-links (never copies, never reads) the MAIN
checkout's `.env` into the worktree via a *relative* symlink when it can find one —
`git rev-parse --git-common-dir` resolves to the main checkout's `.git` regardless of which
worktree invoked it, and comparing it against `--git-dir` is git's own documented worktree
test. When no main `.env` exists either, `require_env_file_or_die()` refuses a real (non
`--dry-run`) `start` with a diagnosis naming the main checkout, instead of proceeding with every
interpolated var empty and letting Postgres crash-loop on an unrelated-looking
"superuser password is not specified".

**`up --wait` returning 0 is not sufficient proof the stack is up (issue #962).** It has a known
race (moby/compose) where a container stuck in a restart loop can be observed "Running" at poll
time. `verify_stack_health()` re-inspects every container this invocation's compose project
controls (`docker compose $COMPOSE_FILES ps -a -q`) after `up` returns, and fails the whole
`start` if any is `restarting`/`exited`/`dead`/`unhealthy`. A container still `health: starting`
is never a failure — Docker's own healthcheck state machine already won't flip that to
`unhealthy` before the service's OWN `start_period` (Keycloak's 120s, the backend's 600s, ...),
so nothing here re-implements a competing global timeout.

- `.fresh/<name>.yml` — the ONLY generated compose overlay. It re-pins every hard-coded
  `container_name` to `otfresh-<name>-*` (`FRESH_NAMED_SERVICES` in `opentr.sh`, plus the aux
  services below when their flag is passed). It also carries two `environment:` additions
  (issues #961b / #968), both generated — never hand-edit this file:
  - `frontend` always gets `CHOKIDAR_USEPOLLING=true` / `CHOKIDAR_INTERVAL` (default 300,
    tunable via that env var before `start`). Two Vite dev servers side by side (this stack +
    the main one) exhaust `fs.inotify.max_user_instances` (128 on a typical host) and the
    second one dies with `EMFILE` watching `vite.config.ts`. Raising the sysctl needs root; a
    `--fresh` stack is by definition the second watcher, so it's the one that polls instead.
  - `backend` gets an offset-aware `CORS_ORIGINS` (a JSON array — pydantic-settings JSON-decodes
    a `list[str]` env var BEFORE its `mode="before"` validator runs, so a comma-separated value
    raises `SettingsError` at startup) whenever `--port-offset` is non-zero. It was the #968 fix
    for the WebSocket origin check (`_origin_is_allowed`, `backend/app/api/websockets.py`) at a
    time when the Vite proxy rewrote `Host`; the `/api/ws` proxy now forwards the browser's
    `Host` (`changeOrigin: false`), so the same-origin rule admits the offset origin and any
    LAN address of it on its own. The list stays as belt and braces.
- `.fresh/<name>.offset` — the recorded `--port-offset` (plain integer, absent = 0). Read on re-up,
  `status --fresh`, and `fresh-list`; deleted by `fresh-destroy` and by `--port-offset 0`.
- `.fresh/<name>.aux` — the aux overlay files the deployment was started with, one per line.
  `fresh_compose_chain()` replays them so `stop`/`status`/`fresh-destroy` address the same chain the
  deployment was created with. **Required, not cosmetic:** without it the generated overlay re-pins a
  `container_name` for a service compose can no longer see and the whole chain is rejected.
- **`--port-offset` is env-var driven, never an overlay.** `fresh_apply_port_offset()` exports the
  `*_PORT` variables the compose files already interpolate (`FRESH_PORT_VARS`: FRONTEND 5173,
  BACKEND 5174, FLOWER 5175, POSTGRES 5176, REDIS 5177, MINIO 5178, MINIO_CONSOLE 5179, OPENSEARCH
  5180, OPENSEARCH_ADMIN 5181, DOCS 5183). **Never reintroduce a generated `-ports.yml`:** compose
  *appends* port lists across files, so a second `ports:` entry publishes the base port too and the
  "isolated" stack collides with the main one (issue #343). A shell env var beats `.env` for
  interpolation, and a value already set in `.env` is the base the offset is applied to.
- Adding a published port to any compose file `--fresh` can load? Add its variable to the matching
  `FRESH_*_PORT_VARS` array in the same commit, or `--port-offset` silently leaves that one service
  on the main stack's port.
- The pre-flight bind check runs at **every** offset (not just 0) and refuses to start if any
  resolved port is taken, so a bad offset fails before `compose up`.

### Aux test overlays under `--fresh` (issue #347)

`--with-ldap-test` / `--with-smb-test` / `--with-monitoring` / `--with-keycloak-test` /
`--with-authentik-test` are fully isolated: ports via
`FRESH_{LDAP,SMB,MONITORING,KEYCLOAK,AUTHENTIK}_PORT_VARS`, container names via
`FRESH_{LDAP,SMB,MONITORING}_SERVICES` fed into the generated overlay, volumes via the project
name. Keycloak and Authentik declare no `container_name` in their compose files (the compose
project namespaces them automatically), so neither needs a `_SERVICES` entry — only ports and the
aux-file record. `--with-mock-llm` / `--with-mock-asr` / `--with-llm-test` / `--with-pki` /
`--with-diar-native` are isolated the same way (`FRESH_{MOCK_LLM,MOCK_ASR,LLM_TEST,PKI}_PORT_VARS`,
`FRESH_{MOCK_LLM,MOCK_ASR,DIAR_NATIVE,LLM_TEST}_SERVICES`); `--with-watch` / `--with-backup` are the
exemptions (below).

- **`LDAP_TEST_PORT` / `LDAP_TEST_UI_PORT`, never `LDAP_PORT`.** `LDAP_PORT` is the *application's*
  LDAP client port (`.env` ships `LDAP_PORT=636`); offsetting it would repoint the app's LDAP config.
- **Renaming a container is only safe because in-network DNS uses the SERVICE name.** Compose
  aliases each service name on the network (`backend`, `prometheus`, `postgres`, `smb-test`), which
  is what Prometheus' scrape target, Grafana's datasource URL and the SMB docs all use. LLDAP is the
  exception — it is documented/configured as `ldap://lldap-test`, the *container* name — so
  `docker-compose.ldap-test.yml` pins `lldap-test` as an explicit network **alias**. Never drop that
  alias, and grep before renaming anything else.
- The ldap/smb/keycloak overlays used to declare the default network `external: true` under the name
  `${COMPOSE_PROJECT_NAME:-opentranscribe}_default`. Compose defaults the project name to the
  **directory**, not `opentranscribe`, so that name never existed and `up` died with "network …
  declared as external, but could not be found". They now use the project's implicit default
  network, like the monitoring overlay always has. The cost: they can no longer be run standalone
  with a bare `docker compose -f docker-compose.ldap-test.yml up` — load them through `./opentr.sh`.
- Every aux overlay publishes on **`127.0.0.1` only**. They are throwaway services with published
  credentials (LLDAP admin_password + hard-coded JWT secret, Samba testuser/testpass, Grafana
  admin/admin, Keycloak admin/admin, unauthenticated Prometheus with `--web.enable-lifecycle`).
  Do not move them to `0.0.0.0`; front them with a proxy instead.
- Keycloak needs `KC_HEALTH_ENABLED: "true"` for its healthcheck. Since Keycloak 25 the health
  endpoints moved to the **management port 9000** and are only served when that flag is set — probe
  `/health/ready` on 8080 and you get a 404 forever, so `up --wait` fails on a Keycloak that works.
- `--with-watch` / `--with-backup` are the remaining un-isolatable overlays and the reason the
  combined-flag warning still exists: they bind **live host directories**, which a fresh stack then
  shares with the main one. Nothing in `opentr.sh` can namespace a path the operator asked for.
- `setup-watch-source-test-data.sh` resolves containers through `OT_CONTAINER_PREFIX`
  (default `opentranscribe`) — pass `OT_CONTAINER_PREFIX=otfresh-<name>` to seed a fresh deployment.

### Fresh / isolated deployments (data-safety guardrails)

**Use `--fresh` for any throwaway or experimental stack — never develop against the live NAS data again.** A fresh deployment runs in its own `otfresh-<name>` compose project (separate containers AND named volumes) and the NAS/bind overlay is **never** loaded, so the real dataset can't be touched.

```bash
./opentr.sh start dev --fresh test1 [--port-offset 100] [--seed-benchmark]
./opentr.sh stop|status --fresh test1        # stop keeps volumes
./opentr.sh fresh-list                       # list fresh deployments + volumes
./opentr.sh fresh-destroy test1              # containers+volumes (y/N; the ONLY destructive fresh op)
./opentr.sh data-paths                       # resolved LIVE data paths — CHECK before deleting anything
./opentr.sh start dev --dry-run              # print compose files + command, start nothing
```

`--fresh` refuses to start when any port it needs is already bound (it offers `--port-offset N`) and generates a gitignored `.fresh/<name>.yml` overlay that re-pins every service to `otfresh-<name>-*`. `--port-offset` works by exporting the `*_PORT` vars the compose files already read — never by overlaying a second `ports:` list, which compose would append (issue #343). The offset is remembered in `.fresh/<name>.offset`. **Every** `--with-*` overlay is isolated too (issue #347) — names, ports, and volumes all move — and the overlays used are recorded in `.fresh/<name>.aux`. Do not maintain the list here by hand: it was stale for four overlays at once, and `--with-llm-test` was missing from `opentr.sh`'s dispatch itself, so a fresh stack collided with the main one on `opentranscribe-llm-test-vllm`/5195 and `fresh-destroy` left a multi-GB vLLM holding a GPU. `backend/tests/unit/test_opentr_fresh_aux_isolation.py` now enumerates the flags and fails on any that is neither isolated nor **explicitly exempt with a written reason**. The three exemptions are `--with-watch` / `--with-backup` (they bind live host directories and declare no `container_name` or ports, so there is nothing project-scoped to re-pin — `opentr.sh` warns instead) and `--with-scratch-tmpfs` (it only changes the driver of the already project-namespaced `pipeline_scratch` volume). `--with-pki` is **not** exempt: its overlay publishes `PKI_HTTPS_PORT`, so it is isolated like the rest. ⚠️ `LLM_TEST_GPU_DEVICE_ID` is deliberately **not** offset: a port offset must never renumber a physical card. Details: `scripts/CLAUDE.md`.

**NAS overlay** (non-fresh `start`): auto-detected from `.env`, announced with a `💾 NAS overlay AUTO-LOADED` banner; `--no-nas` suppresses, `--nas` opts in explicitly. When active it writes a `.opentranscribe-live-data` marker into each bind dir — **if you see that marker, you are looking at live data; do not delete.** Full map: `docs-site/docs/operations/fresh-deployments.md`.
