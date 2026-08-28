---
title: Full Application Test Matrix
---

# Full Application Test Matrix

See also: [Releasing](releasing.md), [Testing](testing.md).

This is the **local** test matrix: everything worth running against a real stack before you
trust a change, staged so the cheap, CI-safe checks run first and the expensive, GPU-hungry ones
run last. [`scripts/release.sh`](releasing.md) is the **release** pipeline — its 12 stages exist
to cut and publish a version, not to re-derive test coverage. The two compose rather than
duplicate: Stage 1 here is what `release.sh preflight <v>` and `verify <v>` already run, Stage 3's
fresh-install/upgrade legs *are* `release.sh rehearse <v>`, and Stage 4 here is nothing more than
"confirm `scan`/`build`/`publish`/`promote` are wired" — it does not reimplement them. Run this
matrix on a branch before opening a PR; run `release.sh` when you are actually cutting a version.

Every leg here wraps an existing script. This doc does not introduce new test logic — it
sequences `scripts/validate-deployments.sh`, `scripts/run-integration-tests.sh`,
`scripts/run-auth-e2e.sh`, `scripts/diar-native-smoke.sh`,
`scripts/release-tests/{test-fresh-install,test-upgrade}.sh`, and `scripts/release.sh` itself.
`scripts/test-matrix.sh` is a thin dispatcher over exactly this table — see
["Anti-staleness"](#anti-staleness) below.

## Coverage stance at a glance

Read this table before assuming a mode is covered — several gaps below were only found because
someone assumed "the matrix covers it" without checking which stage actually asserts what. Each
gap-hunt pass on this repo has found new ones; treat this table as a living record, not a
finished checklist. "Real" means the leg drives the actual application path (upload, restore,
login) and asserts on outcome, not just that a command exited 0.

| Mode | Stage / leg | Coverage | Known gaps |
|---|---|---|---|
| Dev — baseline | Stage 2A | Real: upload, e2e, chat vs mock + real vLLM, all 3 auth IdPs | Leg 4 (real vLLM) is **not reproducible on a single 12 GB GPU** — the default model does not fit at any documented setting ([#608](https://github.com/attevon-llc/OpenTranscribe/issues/608)); see Cycle 2A below for the numbers and the (unverified) `--enforce-eager` escape hatch |
| Dev — GPU scaling | Stage 2B | Real: N-worker topology, concurrent uploads, OOM check | [#609](https://github.com/attevon-llc/OpenTranscribe/issues/609): the Flower leg only proves the worker answered a `?refresh=1` broadcast, not that N workers are registered in the plain `/api/workers` snapshot — that endpoint is a one-shot cache taken at Flower's own process startup and is never refreshed on a timer, so an unrefreshed read can omit a healthy worker forever |
| Dev — diarization | Stage 2C | Real: diar-native default path + PyAnnote fallback | — |
| Dev — lite/CPU | Stage 2D | **Topology-only** — proves no GPU worker/memory, uploads nothing | The pipeline itself (ASR/search/chat) is NOT exercised here — see the lite-mode rehearsal row below |
| Prod — fresh install | Stage 3 | Real: full install against a built image | — |
| Prod — upgrade | Stage 3 | Real: version upgrade path | — |
| Prod — backup/restore/rollback | Stage 3 (`test-upgrade.sh` phases 06b, 12–17, [#598](https://github.com/attevon-llc/OpenTranscribe/issues/598)) — this leg now runs to completion across all 18 phases; two harness bugs that used to silently truncate it mid-run before ever reaching here are fixed ([#617](https://github.com/attevon-llc/OpenTranscribe/issues/617), [#618](https://github.com/attevon-llc/OpenTranscribe/issues/618)) | Real: `opentranscribe.sh backup`/`restore` (issue #613 — the shipped production command; the rehearsal staged `opentr.sh` for this until #613, which was itself an invalid bare-`docker-compose` invocation outside a repo clone) and `update --rollback`, damage injected via the real API, restore asserted by content digest (not row count) | `backup --encrypt` (unattended gpg needs a passphrase file the CLI doesn't support); the in-app scheduled-backup system (`backup_service.py`) has real unit/API coverage but no end-to-end restore proof — see [#604](https://github.com/attevon-llc/OpenTranscribe/issues/604) for its one known remaining defect (gnupg missing from the backend image); MinIO/OpenSearch restore is not touched by the DB restore path; phases 15/17's before/after digest comparisons can still race an async DB write settling after the harness's synchronization point — for the `speaker` table, gender-attribute writes are closed by waiting on `attributes_predicted_at IS NOT NULL` (#617's fix), and LLM speaker-suggestion writes are closed by waiting on no pending/in-progress `speaker_identification` Task row plus 3 consecutive stable digest polls (#620 item 5, a best-effort settle for any other writer, not a guarantee); `media_file` content and `system_settings.embedding_normalization_done` remain open ([#619](https://github.com/attevon-llc/OpenTranscribe/issues/619), non-blocking — every product-level integrity assertion in the same run passed) |
| Prod — lite-mode pipeline | Stage 3 (`test-lite-mode.sh`) | Real: full upload→ASR→search→chat against mocked cloud ASR + mocked LLM, no GPU/vendor key needed | Mock's per-request `?scenario=` override isn't reachable from `GladiaProvider` — the negative-path check restarts the mock container instead of driving it per-request |
| Prod — PKI/mTLS | Stage 3 | Real: client-cert auth, cert-less request rejected at the TLS layer | Prod+nginx only by design — no dev-mode variant exists (Vite can't terminate mTLS) |
| Prod — lite/gpu-scale as deployment modes | — | Compose-validated only (Stage 1.6) | No separate prod runtime pass — deliberate scope decision, since prod images behave identically to dev images for these flags and Stage 2 already proves the runtime behavior |
| Offline / air-gapped | Stage 1.6 | Config-validated only | No real network-namespaced offline install pass exists — do not read Stage 1.6 as proving offline mode works end to end |
| Image/release gates | Stage 4 | Real: scan/build/publish/promote wiring | `scan`'s security-tooling check is warn-severity, not blocking — verify trivy/grype/syft are on `PATH` before relying on it |

**Pattern to watch for**: nearly every gap this repo has found (#598–#604) was a script or
feature that looked covered because *something* referenced it — a doc section, a `RUN_*` env var,
a constant — but nothing exercised the actual failure path. When adding a new script or rehearsal
leg, ask "what does this look like when it silently does nothing, or does the wrong thing and
still exits 0?" and write that test first.

## Stage 1 — Static / no live stack

**~6-9 min. CI-safe: needs no GPU and no running stack.**

| # | Command | Pass criterion |
|---|---|---|
| 1 | `scripts/safe-precommit.sh run --all-files` | Exit 0, no `files were modified by this hook` |
| 2 | `./scripts/run-backend-tests.sh --summary` | Exit 0, 0 failures |
| 3 | `python3 scripts/audit-tests.py backend/tests` + `python3 scripts/audit-tests.py --selftest`; `cd frontend && npm run test:audit && npm run test:audit:selftest` | Exit 0, no `SELF-TEST BROKEN`, DEFERRED (backlog) count not increased vs the prior run |
| 4 | `./scripts/frontend-check.sh --no-claude --check-only` | Exit 0 |
| 5 | `cd docs-site && npm run build` | Exit 0 |
| 6 | `./scripts/validate-deployments.sh --json` | Every permutation `ok`, no "documented flag with no matrix entry" |
| 7 | `python3 scripts/release/check-version-consistency.py` | All version sources agree, single Alembic head |
| 8 | `backend/venv/bin/python3 scripts/audit-route-coverage.py --json` | Uncovered-route count not increased vs the prior run |

This is exactly what `./scripts/release.sh preflight <v>` and `verify <v>` already run as part of
cutting a release. Run this stage standalone when you are **not** cutting a release; the release
pipeline runs the equivalent automatically as part of its own gates.

## Stage 2 — Dev-mode integration

**~2.5-4h total across four stack cycles.** Each cycle is a separate `./opentr.sh start dev ...`
invocation because the overlay combinations genuinely conflict (see 2B) or because isolating them
keeps a failure attributable to one leg.

### Cycle 2A — baseline + LLM + auth (one stack start, ~90-120 min)

```
./opentr.sh start dev --with-mock-llm --with-llm-test --with-ldap-test --with-keycloak-test --with-authentik-test
```

These five overlays safely co-run on one stack: each binds a distinct loopback port (mock-llm
`5199`, `--with-llm-test`'s vLLM `5195`, lldap `3890`/`17170`, Keycloak `8180`, Authentik `9022`),
none of them touches Celery worker scaling or `COMPOSE_PROFILES` (that's Cycle 2B's job), and only
`--with-llm-test` takes a GPU — pinned via `LLM_TEST_GPU_DEVICE_ID`.

⚠️ **`LLM_TEST_VLLM_GPU_UTIL <= 0.45` alone does NOT make the default model fit a 12 GB card — it
does not fit at ALL, at any tested setting** ([#608](https://github.com/attevon-llc/OpenTranscribe/issues/608)).
Measured on an idle RTX 3080 Ti (11.62 GiB visible to the container, every other GPU-resident
container stopped first so the *entire* card was free):

- Loading the model's weights alone (`--dtype float16 --quantization awq`, both hardcoded in
  `docker-compose.llm-test.yml`) consumes **~8.85–9.3 GiB**, independent of
  `--gpu-memory-utilization` — that flag only bounds the *planned* KV-cache pool, never weight
  loading.
- With weights loaded (~9.32 GiB) and the default `VLLM_COMPILE` mode's inductor autotuning then
  running, the engine crashes trying to allocate **another ~1.25 GiB** of scratch
  (`~10.87 GiB in use` when the allocation is attempted) — a floor of roughly **~10.6–10.9 GiB**
  against the 11.62 GiB visible, and this crash is not bounded by `--max-num-batched-tokens`,
  `--max-num-seqs`, or `--max-model-len` either.

So on a single-usable-12GB-GPU host, leg 4 (real vLLM) of this cycle is currently **⊘ NOT
MEASURED** — do not spend time chasing a `GPU_UTIL` value that will fit; none does. Two options,
neither verified yet:

1. Set `LLM_TEST_VLLM_EXTRA_ARGS=--enforce-eager` (compose passthrough added alongside this doc
   fix) to disable `torch.compile`/CUDA graph capture — the standard vLLM VRAM workaround, and the
   escape hatch that did not exist when #608 was measured. This removes the ~1.25 GiB compile
   overhead but was **not** re-measured against a 12 GB card; confirm it actually fits before
   trusting it as the new guidance.
2. Point `LLM_TEST_MODEL` at a smaller model that is known to fit instead.

On a **multi-GPU** host (this project's documented reference hardware: GPU 1 for
transcription/diarization, GPUs 0/2 reserved A6000s with 49 GiB each), point
`LLM_TEST_GPU_DEVICE_ID` at one of the larger idle cards — the default model is proven there
(compose file header) — or sequence the LLM legs (3-4 below) after the transcription-heavy legs
(1-2) finish if only the transcription GPU is available.

Run these legs serially against that one stack. LLM provider and auth method are both
single-valued DB-backed `SystemSettings`, so concurrent legs would race each other's config —
each leg restores its own configuration on exit.

| # | Command | Pass criterion |
|---|---|---|
| 1 | `./scripts/run-integration-tests.sh --coverage --search-quality --cleanup` | Exit 0; the search-quality phase reports a non-zero collected-test count |
| 2 | `./scripts/e2e/run-e2e.sh` | 0 failed. Visual-regression baseline failures must be explicitly triaged before release — never ignored as "known flaky" |
| 3 | `pytest backend/tests/e2e/test_chat.py test_chat_grounding.py test_chat_trace_panel.py` against `mock-gpt`/`mock-echo`/`mock-error`/`mock-reasoning` | Citations resolve, redaction masks apply, SSE completes, each error model surfaces the error it models |
| 4 | Same three files, provider repointed at `http://llm-test-vllm:8000/v1` | Real citations resolve to real segment ids; the local-provider redaction exemption fires (no masking of local-model input). **⊘ NOT MEASURED on a single 12 GB GPU** — the default model does not fit at any tested setting ([#608](https://github.com/attevon-llc/OpenTranscribe/issues/608), see above); run it on a multi-GPU host or a confirmed-fitting `LLM_TEST_MODEL` |
| 5 | `./scripts/run-auth-e2e.sh --cleanup --skip-pki` (PKI is Stage 3 only — no dev-mode PKI variant exists) | Per-method summary green; `GET /api/auth/session` returns 200 anonymous afterward, proving config was restored |
| 6 | — | The `RUN_*`-gated security suites (both FIPS modes) already run inside leg 1's `run-integration-tests.sh` — do not re-run them separately here |

### Cycle 2B — GPU scaling (separate stack cycle, mandatory, ~30-45 min)

```
./opentr.sh stop
./opentr.sh start dev --gpu-scale
```

This is a genuine conflict with 2A, not a convenience separation: `--gpu-scale` sets
`COMPOSE_PROFILES=gpu-scale` and swaps in `docker-compose.gpu-scale.yml`'s worker topology (N
parallel Celery workers pinned to `GPU_SCALE_DEVICE_ID`), which is additive with none of 2A's
overlays. On a single-GPU host, set `GPU_SCALE_DEVICE_ID` to that GPU and stop `--with-llm-test`'s
vLLM first — both want VRAM.

Run via `scripts/gpu-scale-smoke.sh`. `docker-compose.gpu-scale.yml` runs exactly ONE celery
process (`gpu-scaled@%h`) with `--concurrency=GPU_SCALE_WORKERS`, so "N workers register in
Flower" was never the right pass criterion — it checks the pool's `max-concurrency` on that one
process instead, plus the optional default worker (`gpu-transcription@%h`) when
`GPU_SCALE_DEFAULT_WORKER=1`. Pass: the `gpu-scaled@*` worker is present in Flower's
`/api/workers?refresh=1` (⚠️ **not** the unrefreshed `/api/workers` — that endpoint is a one-shot
snapshot cached at Flower's own process startup and never re-inspects on a timer, so a worker
still importing torch/whisperx when Flower booted is absent from it forever; see
[#609](https://github.com/attevon-llc/OpenTranscribe/issues/609)) with `stats.pool.max-concurrency
== GPU_SCALE_WORKERS`, at least 3 concurrent uploads all reach `completed`, no CUDA OOM string in
`celery-worker-gpu-scaled` logs during the run, and batch wall-clock is less than N times a
single-file baseline.

### Cycle 2C — diarization providers (~20 min, can fold into 2A if VRAM allows)

diar-native loads by **default** — `--no-diar-native` is what suppresses it — so Cycle 2A already
exercises it; this is not a separate opt-in overlay. Add:

```
./scripts/diar-native-smoke.sh
```

Pass: the diar-native container holds non-zero device memory and shows no restart loop. Then run
one transcription with `--no-diar-native` to prove the PyAnnote fallback still works. Pass:
completes, speakers assigned.

### Cycle 2D — lite / CPU-only (~20 min)

```
./opentr.sh stop
./opentr.sh start dev --lite --fresh litecheck --port-offset 200
```

Run via `scripts/lite-smoke.sh`. Pass: stack healthy, no `celery-worker-gpu*` container in
`docker ps`, no stack process holding memory on any GPU (`nvidia-smi`). A transcription pass needs
a cloud ASR key — if one isn't configured, record `⊘ NOT MEASURED`, never a pass. Cover `--cpu`
mode with the same script and the same criteria.

⚠️ **This cycle is TOPOLOGY-only — it does not exercise the pipeline.** `lite-smoke.sh` proves the
absence of a GPU worker and of resident GPU memory, and needs a real cloud ASR key to go further
than that. It does **not** upload a file, run ASR, index it, search it, or chat over it. The actual
upload -> ASR -> segments/speakers -> search -> chat pipeline for a lite deployment is covered
separately, with no vendor key required, by the **"3-lite" leg** below
(`scripts/release-tests/test-lite-mode.sh`) — see that section for what it asserts.

## Stage 3 — Deployment mode (prod)

**~3-5h. Requires the dev stack STOPPED.**

```
./opentr.sh stop
BUILD_MODE=local PUSH_LATEST=false ./scripts/docker-build-push.sh all
./scripts/release-tests/test-fresh-install.sh --yes
REQUIRE_PREVIOUS=1 ./scripts/release-tests/test-upgrade.sh --yes
```

Pass: every assertion in each scenario's `REPORT.md` is `PASS`. This sequence is exactly what
`./scripts/release.sh rehearse <v>` runs — **that is the preferred invocation**, since it owns the
ledger and records the run against a real version. Use the raw commands above only when
rehearsing outside a release cut.

### Stage 3 — lite-mode full rehearsal

**~30-45 min. Requires the dev stack STOPPED** (same one-liner-defaults constraint as
`test-fresh-install.sh`/`test-upgrade.sh` — see `scripts/release-tests/README.md`).

```
./opentr.sh stop
./scripts/release-tests/test-lite-mode.sh --yes
```

Runs the real `docker-compose.lite.yml` (no-GPU, cloud-ASR-only) topology against a **mocked**
cloud ASR provider (`scripts/mock-asr-server.py`, a Gladia stand-in) and a **mocked** LLM
(`scripts/mock-llm-server.py`), so it needs no GPU, vendor API key, or network egress. Where Cycle
2D's `lite-smoke.sh` only proves the no-GPU **topology**, this leg drives the real pipeline: ASR
config creation, file upload, transcription completion against the canned mock transcript, segment
count / speaker count / distinctive-token assertions, hybrid search, an OpenSearch ML
deployed-model check, a chat turn against the mocked LLM with a real citation, the Alembic head,
and a negative-path (`MOCK_ASR_SCENARIO=error`) upload reaching `error` status with no leaked
credential in the error message.

Pass: every assertion in `REPORT.md` is `PASS`. Known gap, deliberately deferred (see
`backend/tests/CLAUDE.md`): the mock's per-request `?scenario=` override is not reachable from
`GladiaProvider` itself (issue tracked in the "known deviation" note in
`backend/tests/integration/test_lite_mode_mocked_providers.py`), so this leg's negative-path check
restarts the `mock-asr` container with `MOCK_ASR_SCENARIO=error` mid-run rather than driving it
per-request — safe here because, unlike the shared pytest module, this script owns its own compose
lifecycle serially.

**PKI/mTLS is prod+nginx ONLY.** There is no dev-mode variant and none should be invented — Vite
cannot terminate mTLS.

```
./scripts/pki/setup-test-pki.sh
./opentr.sh start prod --build --with-pki
RUN_PKI_E2E=true pytest backend/tests/e2e/test_pki.py -v
```

Pass: a client cert from `scripts/pki/test-certs/clients/*.p12` authenticates at
`https://localhost:5182`; a request with no cert is rejected at the TLS layer (nginx), not served
as an anonymous 200.

**Scope decisions, stated explicitly:**

- No separate prod pass is run for `--lite`/`--gpu-scale`. Compose validity is checked in Stage
  1.6, runtime behavior in 2B/2D, and prod images behave identically to dev images for those
  flags. This is a deliberate scope decision, not an oversight.
- Offline/air-gapped deployment is **config-validated only** (Stage 1.6). A real
  network-namespaced offline install pass is a known, currently uncovered gap — do not read
  Stage 1.6 as proving offline mode works end to end.
- **`opentranscribe.sh backup`/`restore` and `opentranscribe.sh update --rollback` ARE rehearsed**,
  by `test-upgrade.sh`'s phases 13-17 ([#598](https://github.com/attevon-llc/OpenTranscribe/issues/598)).
  Phase 12 asserts the rollback precondition (`# OT_PREVIOUS_IMAGE_TAG`) a real
  `update --version` records; phase 15 restores the phase-06b pre-upgrade backup over damage
  inflicted through the real API and asserts content digests, not row counts, match exactly
  (a delete+insert pair leaves counts unchanged); phase 16 runs the real
  `update --rollback` and asserts the FROM image serves the restored FROM database through its
  real API (login, file list, transcript text) — not merely that the command exited 0; phase 17
  proves the documented recovery loop (roll back -> re-upgrade) completes cleanly. A
  `ROLLBACK_INJECT_FAULT` self-check (`truncate`/`no-damage`/`stale-oracle`) deliberately breaks
  the tail so its own failure detection is exercised for real — see
  `scripts/release-tests/selftest-rollback-fault-injection.sh`. **Still NOT covered, deliberately**:
  `backup --encrypt` (unattended `gpg` needs a passphrase file the CLI does not support), the
  in-app scheduled-backup system (`app/services/backup_service.py` — a separate implementation
  with its own real unit/API coverage but no end-to-end restore proof), and MinIO/OpenSearch
  restore (the DB restore does not touch either — asserted as R-11, not merely unclaimed). Full
  detail: `scripts/release-tests/README.md`.

## Test data & fixtures

What each stage actually uploads/transcribes, for developers extending this matrix or writing a
new rehearsal leg — none of it needs network access or a committed binary asset beyond what's
already in the repo:

| Stage / leg | Data source | Why |
|---|---|---|
| Stage 1, Cycle 2C | none (config/compose validation, or GPU memory check only) | no media needed |
| Cycle 2A (e2e, chat) | `backend/tests/e2e/conftest.py`'s `owned_media_factory` — uploads an ephemeral file per test, deleted in its own teardown | e2e tests must never persist changes to dev data (root `CLAUDE.md`) |
| Cycle 2A legs 3-4 (chat) | canned scenario responses from `scripts/mock-llm-server.py` (`mock-gpt`/`mock-echo`/`mock-error`/`mock-reasoning`), or a real `llm-test-vllm` model for leg 4 | exercises the app's actual error handling, not a stub that always succeeds |
| Cycle 2D | none — `lite-smoke.sh` is topology-only (no GPU worker, no resident GPU memory); it does not upload anything | see the "TOPOLOGY-only" warning above |
| Stage 3 fresh-install / upgrade | `scripts/release-tests/provision-test-media.sh` — derives small (under 5 MB) real-speech clips from real-speech assets **already in the repo** via `ffmpeg`, never a network fetch. Rejects the e2e suite's synthetic 440 Hz sine and `watch/podcast.mp3` (both silent-of-speech) specifically because the scenario's core assertion is "transcript is non-empty," which a tone or synthetic clip can't honestly prove | reproducible on any checkout, no decaying external URL, and the assertion means something because the source audio genuinely has speech |
| Stage 3 lite-mode | `scripts/mock-asr-server.py`'s canned Gladia-shaped response, reshaped from `backend/tests/fixtures/media/sample_transcript.json`, containing the distinctive token "Zylofenix" so search/citation assertions can confirm the right content actually round-tripped | no GPU, vendor API key, or network egress required for the whole `--lite` pipeline rehearsal |
| Stage 3 PKI | `scripts/pki/test-certs/clients/*.p12`, generated by `scripts/pki/setup-test-pki.sh` (gitignored, contains private keys — never commit) | real client-cert auth, not a stub |

If a future leg's fixture needs are **not** covered by one of the above (e.g., a rehearsal that
needs a specific language, a specific speaker count, or a specific duration), extend
`provision-test-media.sh`'s `SOURCES` list or add a new derivation step there rather than
committing a new binary fixture or reaching for a network URL — that script's header explains
the reasoning in full.

## Stage 4 — Image/release gates

**~45-90 min. Mostly already automated — this stage is "confirm existing automation covers it,"
not new work.**

```
./scripts/release.sh scan <v>
./scripts/release.sh build <v>       # multi-arch; needs USE_REMOTE_BUILDER=true, else 2-3h under QEMU
./scripts/release.sh publish <v>
./scripts/release.sh promote <v>
```

`scan`'s security-tooling check is a **warn**-severity preflight, not a blocking one — a missing
`trivy`, `grype`, or `syft` on `PATH` silently reduces scan coverage rather than failing the
stage. Verify all three are installed before starting Stage 4.

## Time budget

| Stage | Time |
|---|---|
| Stage 1 | 6-9 min |
| Cycle 2A | 90-120 min |
| Cycle 2B | 30-45 min |
| Cycle 2C | 20 min |
| Cycle 2D | 20 min |
| Stage 3 (fresh-install + upgrade) | 3-5 h |
| Stage 3 (lite-mode rehearsal) | 30-45 min |
| Stage 4 | 45-90 min (2-3 h if the remote builder is unavailable) |
| **Full matrix** | **~7-9 h — realistically one working day with triage** |

Sequencing: Stage 1 + Cycle 2A fit in one day. Cycles 2B/2C/2D plus Stage 3 fit in a second day.
Stage 4 happens naturally as part of an actual `release.sh run` — don't schedule it separately.

## Anti-staleness

`scripts/test-matrix.sh` parses this document's leg tables and fails loudly if a documented leg
has no matching implementation in the script, or vice versa — the same technique
`scripts/validate-deployments.sh` uses to keep its deployment matrix from drifting out of sync
with `opentr.sh`. Don't add a row here without adding its leg to the dispatcher in the same
change, and don't add a leg to the dispatcher without a row here.

## CI/CD readiness

- Every leg is non-interactive (`--yes` bypasses confirmation prompts).
- Exit codes follow `release.sh`'s stable contract: `0` pass, `1` gate failed, `2` misuse, `3`
  precondition unmet, `4` operator abort.
- `--json` output matches `release.sh`'s shape: `{stage, leg, status, criteria[], next[]}`.
- State is held in a gitignored per-run ledger; nothing persists across invocations except that.
- Stage 1 needs no GPU and is the CI-safe subset. Stages 2-4 need a GPU runner and a stack that
  only one job touches at a time — serialize, never fan out in parallel.
- **No AWS-specific logic, credentials, registry names, or pipeline definitions belong in this
  repo.** The core is vendor-clean (see `backend/CLAUDE.md`'s note on the absence of
  `app/services/cloud`). A future consumer — the private `opentranscribe-cloud` repo, or a CI job
  — shells out to `scripts/test-matrix.sh <stage> --json --yes` and reads the exit code. That's
  the entire contract surface this repo owns, in the same spirit
  `scripts/release/release-criteria.yaml` states for its own gates: "If a second consumer ever
  appears (a CI job, an AWS promotion job), add its stage here and wire it the same way —
  bidirectionally, or not at all."
