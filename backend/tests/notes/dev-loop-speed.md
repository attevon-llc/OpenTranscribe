# Dev-loop speed aids

- `./scripts/run-backend-tests.sh --affected [args]`: pytest-testmon 2.3.0 (MIT, dev-only,
  `requirements-dev.txt`) runs only tests whose executed code changed. Uses `--testmon
  --testmon-forceselect` because our `addopts` carry `-m`, which plain testmon treats as "select
  everything" ([testmon.org](https://testmon.org/)); refuses `--cov` (testmon forces nocollect
  under coverage); never writes `last.xml`/`last.meta`; prints `AFFECTED-ONLY RUN ... NOT
  EVIDENCE`. The first run per checkout builds `.testmondata` (gitignored, dockerignored).
- `scripts/rerun-failed.sh <artifact-dir>`: replays a gate phase's recorded argv
  (`<junit>.xml.argv`, written by `run-integration-tests.sh`) for the failed node ids only, under
  the exported `*_PORT` vars of a kept stack. Secret-named env values are never recorded
  (`NAME=@ENV`, re-read at replay).
- Single file or a few files: `-n0` skips xdist start-up; `tests/conftest.py`'s
  `pytest_xdist_auto_num_workers` already sizes `-n auto` to the file count (<= 4 explicit
  files) and defers to `PYTEST_XDIST_AUTO_NUM_WORKERS`
  ([xdist docs](https://pytest-xdist.readthedocs.io/en/stable/distribution.html)).
- **Never in a gate.** `run-integration-tests.sh` and `run-dev-tests.sh` refuse `--testmon`,
  `--lf`, `--ff`, `--sw`, `--picked` in their args or `PYTEST_ADDOPTS` (exit 2), and every gate
  pytest call carries `-p no:testmon -p no:stepwise` (`scripts/lib/no-selection.sh`);
  `unit/test_release_path_never_selects.py` polices the release scripts.
  `~/ot-cleanup-work/gate-wave.sh` needs the same refusal by hand (coordinator's file).
