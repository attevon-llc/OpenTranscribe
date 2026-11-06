# scripts — `run-dev-tests.sh` (quick dev-cycle check)

Moved intact from `scripts/CLAUDE.md` ("Which script to reach for"). Read this before changing or relying on `run-dev-tests.sh`, `run-backend-tests.sh --summary` or the overlay reconciliation.

- **Quick dev-cycle check** — `run-dev-tests.sh --full` (or `--fast`/`--backend-only`/
  `--e2e-only`/`--frontend-only`). Chains `run-integration-tests.sh` → `e2e/run-e2e.sh` →
  `frontend-check.sh` and prints one consolidated pass/fail report with per-phase logs under a
  fresh `/tmp/ot-run-dev-tests.*` dir. **Not** `test-matrix.sh`: this is the fast "does my
  current branch work" loop for ordinary development (minutes), `test-matrix.sh` is the
  exhaustive deployment-mode rehearsal (dev/prod/lite/PKI/GPU-scale/fresh-install/upgrade,
  stages 1-4) run before cutting a release (up to hours). Owns no test logic itself — same
  convention as `test-matrix.sh` below — if a phase needs new behavior, add it to the wrapped
  script. `--fast` swaps the backend phase's `--coverage` for `--e2e-smoke` and skips the full
  e2e phase, for a quicker loop when iterating. Measured `--fast` on this host: ~22 min
  end-to-end, dominated by the integration-marked phase (~9 min of real live-stack roundtrips).
  Mode flags are composable (union their phases). **Overlay auto-orchestration**
  (`scripts/lib/dev-test-overlays.sh`, issue #630): mock-LLM/Keycloak/LDAP test containers
  needed by the requested phase start automatically and tear back down on exit, restoring any
  `auth_config` DB setting (`oidc_enabled`/`ldap_enabled`) they flipped — an overlay already
  running before the script started is left alone. `--all-overlays` also brings up
  `--with-watch` (the only `all`-tier overlay; `mock-asr` and `diar-native` are `auto`-tier, started
  by the phase that needs them); `--with-gpu-scale` exercises the multi-GPU worker topology
  and auto-skips with a clear message on a single-GPU deployment (never auto-started under any
  other flag); `--no-overlays` is the escape hatch (stack already configured, skip all
  auto-detection); `--list-overlays`/`--dry-run` print the resolved plan without starting
  anything. Every `opentr.sh --with-*` flag is either in the overlay table or explicitly
  exempted with a written reason there — `test_run_dev_tests_overlay_coverage.py` fails the
  build on any flag that's neither. **`--with-diar-native` is managed CONDITIONALLY**, the only
  overlay that is: `OVERLAY_NEED_PREDICATE` names a function that must return 0 before the
  overlay is considered needed at all, and diar-native's is `diar_native_sidecar_expected`
  (`scripts/lib/diar-native-expected.sh`) — the *same* predicate `run-integration-tests.sh`'s
  diar-native phase gates on, sourced rather than copied, so the overlay starts exactly when
  that phase would otherwise **fail** for its absence. It was previously exempt on the grounds
  that "no test selector this script drives needs it", which stopped being true when that phase
  landed. It is also exempt from `teardown_overlays` (`OVERLAY_TEARDOWN_EXEMPT`, shared only with
  `watch`, which has no container to stop): unlike a mock provider it is part of a native deployment's normal
  operating configuration, so stopping it leaves a *running* stack silently falling back to
  in-process PyAnnote — worse than never starting it, because the fallback is by design silent
  and the tests have already reported green. `test_dev_test_diar_native_overlay.py` drives the
  real bash resolver with the predicate stubbed both ways; a table entry nothing consults looks
  identical to one that works. `compose_project_name()` (now in `scripts/lib/compose-project.sh`, sourced by that file) resolves the live
  stack's actual compose project from a running `postgres` container's label, not
  `basename $REPO_ROOT` — the latter breaks when this script runs from a git worktree
  (`.claude/worktrees/<name>`), since `$REPO_ROOT` then resolves to the worktree's own
  directory name, not the main checkout's. **Three more phases, all STRICT opt-in — never
  included by `--full`/`--fast`, each also a valid phase selector on its own**:
  `--with-gpu-diarization` wraps `run-diarization-gpu-tests.sh` (see its own entry below —
  builds a dedicated test image, several minutes, needs a visible GPU); `--with-mutation-tests`
  runs a single-module `run-mutation-tests.sh --module` pass (default module `spans`, override
  with `MUTATION_TEST_MODULE=<module>`; never `--all` through this flag — that's hours, run it
  by hand); `--with-summary-eval` runs `scripts/run-summary-eval.sh --inputs smoke` (one I1
  summary per rule set against the same `--with-llm-test` model; it asserts the arm was applied
  and the output is schema-valid, and measures no verdict; NOT MEASURED until the lead's
  `arms/new.json` exists); `--with-pipeline-smoke` runs `tests/e2e/test_full_pipeline_smoke.py` — the one test
  in the whole suite that pushes a real fixture (`backend/tests/fixtures/media/sample_short.wav`,
  10s of real speech) through real upload -> real WhisperX/diarization -> real search indexing ->
  a real local LLM chat answer, start to finish, and asserts on the result. Every other e2e test
  either checks an upload merely lands in the backend (`test_upload.py`, never waits for
  completion) or drives the UI against a file that was already `status == "completed"` from an
  earlier session. It brings up `--with-llm-test` itself (real GPU-backed vLLM on
  `LLM_TEST_GPU_DEVICE_ID`, default GPU 2) if not already running and stops it again on exit if
  this run started it — same "bring it up, tear it back down" shape as `--with-gpu-scale`, except
  this one *does* revert (cheap to stop, unlike the gpu-scale worker topology). All three need
  the live stack's `.env` and (for GPU diarization) the gitignored `benchmark/test_audio/*.wav`
  fixtures and a populated `MODEL_CACHE_DIR` — none of these are checked into a fresh git
  worktree, only symlinked in by hand alongside `.env`/`backend/venv`; a worktree missing any of
  them fails the wrapped script's own precondition check (not a bug in the flag itself). Verified
  live end-to-end on this host: GPU diarization pinned at a **non-default** card via the wrapped
  script's own `DIARIZATION_PROBE_GPU` env var (`DIARIZATION_PROBE_GPU=2
  ./scripts/run-dev-tests.sh --with-gpu-diarization`) — real image build, real container, real
  GPU, 11/11 passed; `--with-pipeline-smoke` from a cold `llm-test-vllm` — real model load, real
  10s-clip transcription (1 segment, 19 words), real diarization, real search hit on a word
  pulled from the actual transcript, real chat answer from the real model — 3/3 passed (99s),
  llm-test-vllm correctly stopped afterward since this run started it.
- ⚠️ **`run-backend-tests.sh --summary` runs NO tests** — it re-reports
  `/tmp/ot-backend-tests/last.xml`. `test-matrix.sh` leg 1.2 is that command, so on 2026-09-06
  the matrix's "backend test summary" leg passed by reading a **986-byte, two-day-old, 5-test**
  artifact from a different commit. `--require-fresh [<sha>]` (leg 1.2 now passes it) refuses an
  artifact with no `last.meta`, from a different commit, or older than `OT_SUMMARY_MAX_AGE_S`
  (default 7200) — missing metadata is a refusal, not a pass. The run path writes `last.meta`
  (`sha=`/`epoch=`) when it publishes.
