# Rehearsal alignment plan — run the SHIPPED commands, not a second implementation

Status: implemented on `release-tests/opentranscribe-sh-alignment`.
This file is the rationale record for that change set — read it before "simplifying"
any of the bring-up sites back into a hand-written `docker compose -f ...` list.

## The problem

A release rehearsal is supposed to prove *a real self-hoster can install and upgrade
this app*. The harness got the exclusion of `opentr.sh` right — that is dev-only
tooling a curl install never has, deliberately absent from `release-manifest.txt`,
with the reason written at `test-upgrade.sh`'s `_stage_manager_at`. But it then
bypassed `opentranscribe.sh` too — **the entry point a real user actually gets**
(`setup-opentranscribe.sh` → `./opentranscribe.sh start`,
`docs-site/docs/getting-started/quick-start.md`) — and hand-built its own
`docker compose -f ...` argument lists instead.

That makes the rehearsal's overlay selection a **second, hand-maintained
implementation** of logic `opentranscribe.sh:get_compose_files()` already owns. The
two had already drifted. Every finding below is a consequence of that one fact.

## Verified findings

Each was re-read against the tree at the time of writing, not taken on trust.

### A — `get_compose_files()`'s entire selection layer was dead code at rehearsal time

`opentranscribe.sh start` calls `get_compose_files()`, which calls
`detect_nvidia_runtime()`, `is_blackwell_gpu()` and `force_cpu_mode_requested()` to
choose between `docker-compose.gpu.yml`, `docker-compose.blackwell.yml` and a
CPU-only chain, then optionally adds the nginx and backup overlays.

The rehearsal instead hardcoded `-f docker-compose.yml -f docker-compose.prod.yml
[-f docker-compose.gpu.yml]` keyed on its own `TEST_USE_GPU`
(`test-fresh-install.sh` phase 04, `test-upgrade.sh` phase 04). Consequences:

* **Blackwell detection was never exercised.** The manifest's own header documents a
  past regression of exactly this shape — the installer never downloaded
  `docker-compose.blackwell.yml`, the `[ -f ]` guard turned that into a silent
  fallback to the generic GPU overlay, and the rehearsal could not have caught it.
* **`--cpu` / `FORCE_CPU_MODE` had zero rehearsal coverage.**
* The nginx/HTTPS and backup overlays are never reached through the real selector.

### B — the model-cache permission fix was reimplemented

`test-fresh-install.sh` phase 03 ran its own `docker run --rm busybox chown -R
1000:999 …` instead of letting the shipped `fix_model_cache_permissions()` (called by
`opentranscribe.sh start`) do it. A regression in the shipped one was invisible.

Note the prior write-up said that function "calls `scripts/fix-model-permissions.sh`".
It does not — it inlines the busybox chown and only *mentions* that script in a
failure hint. The fix is the same either way: call the shipped path.

### C — the upgrade rehearsal ran the wrong script version against the FROM stack

`test-upgrade.sh` phase 04 brought the FROM stack up with a hand-built compose list,
and `_stage_manager_at`/phase 07 copy `$REPO_ROOT/opentranscribe.sh` (HEAD) for every
staged tree including the FROM one. A real user on FROM is running **FROM's** copy of
the script until they run `update-full`, which self-refreshes it
(`docs-site/docs/operations/upgrading.md`).

Measured against the real FROM release (v0.4.1 worktree):

| capability | v0.4.1's `opentranscribe.sh` | HEAD's |
|---|---|---|
| `start` / `get_compose_files` | yes | yes |
| `update --version` | **no** (`update` pulls `:latest` only) | yes |
| `backup` / `restore` arm | **no** (added by #613 in v0.5.0) | yes |
| `${OT_IMAGE_TAG}` in `docker-compose.prod.yml` | **no** (`:latest` hardcoded) | yes |

So the fix is per-site, not blanket:

* **Phase 04 (bring the FROM stack up): use FROM's own script.** It has `start`, and
  that is precisely what a real FROM user runs. Done.
* **Phase 06b (`opentranscribe.sh backup`): feature-detect.** FROM's script has no
  `backup` arm, so it cannot be used — and a v0.4.1 user genuinely cannot run that
  command, which is why `upgrading.md` also gives the raw `pg_dump` recipe and why
  phase 06b already takes *both* artifacts. Prefer FROM's script when it dispatches
  `backup`; otherwise fall back to HEAD's and record why. From v0.5.0 onward FROM will
  have the arm and the rehearsal switches to the faithful path with no edit.
* **Phase 08 (the upgrade itself): keep `update --version $TO` on the TO script.**
  Reasoning, because this is the one that needed a decision:
  - FROM's `update` has no `--version`; it can only `pull` whatever `:latest` is. The
    version under test is unreleased and not on Hub, so FROM's script *cannot*
    reach it. There is no way to run the TO upgrade from the FROM script.
  - `update --version` is the only path that records `# OT_PREVIOUS_IMAGE_TAG`, which
    phases 12/16/17 assert and depend on.
  - `upgrading.md` documents `./opentranscribe.sh update --version vX.Y.Z` as the way
    to move to a specific release.
  - The reason a user *has* the TO script at that point is `update-full`, which
    re-downloads every artifact in `release-manifest.txt`. So the honest fix is not to
    change the command but to make **phase 07 build the after-tree by replaying
    `release-manifest.txt`** instead of a hand-written `cp` list — that is what
    `update-full` does, and it removes a fourth hand-maintained artifact list.

  What remains uncovered, deliberately: **FROM's own `update-full` is not executed.**
  It fetches from `raw.githubusercontent.com/<branch>`, and the release under test is
  the local HEAD, which is not fetchable at that URL. Closing this needs either a URL
  override seam in a shipped script or a local HTTP mirror — a product decision, not a
  harness one. Recorded as a SKIP in the report so it is visible, never silent.

### D — Scenario A never validated the docs image at the release tag

Confirmed, and worse than first described. Scenario A installs with
`OPENTRANSCRIBE_BRANCH=master`, which makes `resolve_install_ref()` write
`OT_IMAGE_TAG=latest` into the install `.env`. Phase 03 then pinned images with a
**hand-maintained per-service list** (`cp_pin_image_tag` over backend/frontend/9
celery services) that does not include `docs` or `celery-worker-gpu-scaled`. So those
two resolved `:latest` — on this host, a four-month-old `davidamacey/opentranscribe-docs:latest`
— and nothing asserted on the docs container at all. Phase 01 also never built the docs
image, so on a host without a stale `:latest` the `docs` service falls back to its
`build: context: ./docs-site`, which the installer never downloads.

Fix: pin **one** line — `OT_IMAGE_TAG` in the install `.env` — which every service in
`docker-compose.prod.yml` already resolves through `${OT_IMAGE_TAG:-latest}`
(statically guarded by `test_every_prod_service_image_is_tag_pinnable`), delete the
per-service list, build/provision the docs image in phase 01, and assert the docs
container is up and serving.

Deliberately NOT changed: `test-upgrade.sh` phase 03 keeps its per-service pins.
v0.4.1's `docker-compose.prod.yml` hardcodes `:latest` with no `${OT_IMAGE_TAG}`, so
the one-line `.env` pin is inert there and the per-service rewrite is the only thing
that works.

### E — `--lite` rehearses a deployment shape no real user can reach

`test-lite-mode.sh` runs `-f docker-compose.lite.yml`, but that file is **not** in
`release-manifest.txt` and `get_compose_files()` has no lite branch. A curl install
therefore never downloads it and can never select it. `README.md` nevertheless
advertises "API-Lite Deployment"; the only documented invocation
(`docs-site/docs/operations/deployment-configuration.md`) is `./opentr.sh start dev
--lite` — the *dev* script. The lite image is also not built by
`scripts/docker-build-push.sh all` (backend, frontend, docs only), so no
`opentranscribe-backend-lite` is published as part of a release.

Making `--lite` genuinely shippable means: manifest entry + a `get_compose_files()`
branch keyed on `DEPLOYMENT_MODE` + publishing the lite image in the release pipeline
+ installer support. That is a product decision and a release-pipeline change, so this
pass takes the safer option: **relabel it honestly** as a repo/dev-only deployment
shape, and add a test that FAILS the moment that stops being true, so the relabel
cannot outlive its subject. Left for the owner to decide the other way.

### F — diar-native is NOT a rehearsal bug (confirmed, and left alone)

`docker-compose.diar-native.yml` is correctly absent from `release-manifest.txt` and
from every rehearsal chain. `opentr.sh`'s auto-load is gated on
`-d /mnt/nvm/repos/diar-native/models_folded`, a dev-host-only path. The separate,
real product finding — `diarizer_backend` defaulting to `"native"` while the sidecar
is unshippable — is out of scope here and belongs in its own issue.

## Change set, in commit order

1. `opentranscribe.sh`: add a `compose-files` command that prints the resolved chain
   on stdout (the existing banners keep going to stderr). One small addition to the
   shipped script, and the seam that makes the selection logic both assertable by the
   rehearsal and unit-testable without GPU hardware.
2. `backend/tests/unit/test_compose_file_selection.py`: exercise
   `get_compose_files`/`detect_nvidia_runtime`/`is_blackwell_gpu`/`force_cpu_mode_requested`
   through that command with `docker`/`nvidia-smi` stubbed on `PATH` — Blackwell,
   non-Blackwell nvidia, no nvidia, forced-CPU, nginx with/without certs, backup
   overlay, and the missing-overlay silent-fallback case. Plus the finding-E guard.
   Follows the existing `backend/tests/unit/test_install_upgrade_scripts.py`
   convention (pytest + subprocess + stubs); this repo has no `.bats` harness.
3. `scripts/release-tests/lib/compose-chain.sh`: `cc_*` helpers to resolve the chain
   through the shipped command, derive the EXPECTED chain independently from host
   facts, assert them equal, and record the result in `REPORT.md`.
4. `test-fresh-install.sh`: phase 01 provisions the docs image; phase 02 passes
   `--cpu` when GPU is off; phase 03 pins `OT_IMAGE_TAG` in `.env` and drops both the
   per-service pin list and the reimplemented chown; phase 04 becomes
   `./opentranscribe.sh start`; phase 05/06 assert the resolved chain, that every
   selectable overlay was actually downloaded, and that docs is serving.
5. `test-upgrade.sh`: phase 03 stages FROM's own `opentranscribe.sh`; phase 04 runs
   `./opentranscribe.sh start` from it; phase 06b feature-detects the `backup` arm;
   phase 07 replays `release-manifest.txt`; phase 08 asserts the resolved chain and
   records the `update-full` coverage gap as a visible SKIP.
6. `test-lite-mode.sh` + `README.md`: the finding-E relabel.

## How each change is validated

* Selection logic: the unit tests above — they must be watched to fail against the
  pre-change tree (`git archive HEAD` into `/tmp`, per the repo's red-check rule).
* Bring-up equivalence: with the install tree staged exactly as the rehearsal leaves
  it, `./opentranscribe.sh compose-files` must resolve to the same chain the old
  hand-built list produced on this host, and `docker compose <chain> config
  --services` must be identical between the two. That is a minutes-long check, not a
  3-hour scenario.
* Everything else: needs a real rehearsal run. What was and was not run end-to-end is
  reported rather than assumed.
