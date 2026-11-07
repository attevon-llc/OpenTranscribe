# scripts/e2e — the E2E runners

Moved intact from `scripts/CLAUDE.md`. Fixtures, markers and `RUN_*` gates: `backend/tests/CLAUDE.md`.

- **E2E** — `e2e/run-e2e.sh`: two phases, non-visual in parallel (`E2E_WORKERS`, default 3) then
  `-m visual` serially. `e2e/run-e2e-smoke.sh` is a 5-file subset of it.
  **pytest exit 5 = "no tests collected" is not a failure for a marker-filtered phase.**
  `resolve_phase()` forgives 5, and *only* 5, and *only* for a caller-selected file subset:
  none of the smoke script's files holds a `visual` test, so phase 2 collected nothing,
  exited 5, and `run-e2e-smoke.sh` **always exited non-zero** — unnoticed because the gate's
  `--e2e-smoke` calls pytest directly instead of going through here. Exit 1/2/3/4 still
  propagate, so a real phase-2 failure fails the run; and on the WHOLE tree 0 collected still
  FAILS, because there it means the `visual` marker itself selects nothing (a renamed marker
  would otherwise delete the entire screenshot suite from the gate in silence).

- `e2e/run-e2e-release.sh` — the v0.6.0 browser journeys; `scripts/release/60-test.sh` runs it on its own
  fresh stack and records the `release-e2e-journeys` criterion (#1282). Add a journey to its file list and to
  `backend/tests/unit/test_release_e2e_selection.py`.
- `e2e/run-e2e-first-run.sh` — the first-run wizard terms step (#1366, marker `first_run`). It consumes the
  wizard's one-time state, so it runs FIRST on a throwaway `--fresh` stack and `run-e2e.sh` excludes the marker.
