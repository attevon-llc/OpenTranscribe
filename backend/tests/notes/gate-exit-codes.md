# backend/tests — the gate's exit codes and the NOT MEASURED phase

Moved intact from `backend/tests/CLAUDE.md`. Read before changing `scripts/run-integration-tests.sh`, `run-dev-tests.sh` or how a phase reports.

## ⚠️ A NOT MEASURED phase is not a pass, and the gate's exit code now says so

`run-integration-tests.sh` prints `⊘ … NOT MEASURED` for a phase that declined to be counted
(mass skips past the ceiling, or a check with no evidence) — and for months it then **exited
0**, so `run-dev-tests.sh` recorded the backend phase `PASS` and `scripts/release/60-test.sh`
recorded `integration-gate pass` for a release, on a run whose largest phase (733 s, 21 skips)
had explicitly refused to count itself. It now **exits 4**; `run-dev-tests.sh` renders a
`NOT MEASURED` row and exits **5**, and `60-test.sh` records `integration-gate not-measured`
(blocking).

⚠️ **The two codes differ on purpose.** In the repo-wide *standard* contract (`release.sh`,
`test-matrix.sh`'s leg contract) **4 already means operator abort**, so a script invoked under
it needs 5 — otherwise an honest "a phase verified nothing" prints as
`ABORT — the leg reported an operator abort`. `run-integration-tests.sh` can use 4 because it
has no prompt and no abort path. Pinned by `unit/test_test_matrix_execution.py`'s
`test_the_two_exit_contracts_are_read_separately` and
`unit/test_integration_gate_skip_ceiling.py`.

Every pytest phase in the gate now runs with **`-rs`** and writes `--junitxml` into
`$GATE_ARTIFACT_DIR` (default `/tmp/ot-integration-gate`), because the 2026-09-06 gate's
21 + 18 + 78 + 56 skips had **no recorded reason anywhere** — diagnosing them meant re-running a
733-second phase by hand.

Two more things the gate does differently, both measured:

- **Every `RUN_*` a gate script sets must be READ by a test — and there is no `GATED_FILES`
  list any more.** ⚠️ This bullet used to describe a `GATED_FILES` array whose entries had to
  carry a `RUN_*` gate, policed by `unit/test_gated_files_all_have_gates.py`. **Both are
  gone**; neither name appears anywhere in `scripts/` or `backend/` today, so do not go
  looking for them. What happened: the module-level `skipif` gates were deleted from all
  eight security suites (each now opens `# Runs by DEFAULT. This module was gated behind
  RUN_<X>_TESTS…`) while `run-integration-tests.sh` kept exporting seven variables —
  `RUN_PKI_TESTS RUN_MFA_TESTS RUN_LLM_TESTS RUN_FEDRAMP_TESTS RUN_FIPS_TESTS
  RUN_AUTH_CONFIG_TESTS RUN_ADVANCED_ADMIN_TESTS` — that **no test read**. A phase named
  "Gated security suites" was setting variables that changed nothing.
  The old guard could not catch it: it substring-matched the variable name over the whole
  file, and all eight files still *mention* their dead variable in that very comment. It
  passed while describing a mechanism that no longer existed — a test that cannot fail, in
  the file written to prevent tests that cannot fail. `unit/test_gate_run_env_vars_are_live.py`
  replaces it and matches **structurally** (AST: the name must be a call argument, a subscript
  index, or an assignment value), so prose cannot satisfy it.
- **ONE FIPS phase, not two.** `FIPS_MODE=true` *is* live (`app/core/config.py` reads it at
  import), so `Security suites (FIPS_MODE=true)` is a real claim and stays. The **FIPS-off**
  half was deleted: with the gates gone those eight files are ordinary members of the Unit/API
  suite, so that phase re-ran, byte for byte, tests phase 1 had just run. Measured on the
  2026-09-07 gate's own junit artifacts — all **394** ids in `gated-fips-off.xml` also appear
  in `unit.xml`, **0 missing** — so removing it removed 394 duplicate *executions* and zero
  tests. `FIPS_MODE_SUITES` in `run-integration-tests.sh` is the current list.
- **`--e2e-smoke` goes through `scripts/e2e/run-e2e-smoke.sh`**, not a bare pytest listing the
  same four files. The old bypass meant the gate skipped `resolve_phase`, the 3 workers and the
  stack preflight — and it is *why* nobody noticed that `run-e2e-smoke.sh` always exited
  non-zero (its phase 2 collects no `visual` test, exits 5, and only `resolve_phase` forgives
  that). Same 39 passed / 1 skipped, 174.4 s -> 158.3 s.
