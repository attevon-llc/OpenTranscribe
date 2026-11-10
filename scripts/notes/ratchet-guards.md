# Ratchet guards (`scripts/guards/`)

Each `check_*.py` is a pre-commit hook (commit or push stage) that compares the tree with
`baselines/<name>.txt` (`key<TAB>value<TAB>reason`). A baseline only shrinks: `--tighten` lowers values and drops
stale lines, it never adds. Things that are legitimate forever live in `allowlists/<name>.txt` instead, each reason
starting with a class tag (`applied-history`, `stub-gap`, `single-file-installer`, ... see `_baseline.ALLOWLIST_CLASSES`);
an allowlist entry with no violation left fails as stale and is deleted by hand.

- `file_size`: `opentranscribe.sh` (2872) and `setup-opentranscribe.sh` (2748) are `single-file-installer` allowlist
  entries (a user-downloaded installer stays one self-contained file). `size_report.py --write-snapshot` refreshes
  `baselines/size_stats.json`; allowlisted files are excluded from its debt figures.
- `guard-svelte-unused` (push stage, ~50 s): `svelte-check` with `frontend/tsconfig.unused.json`
  (`noUnusedLocals`/`noUnusedParameters`), `.svelte` files only. The eslint `no-unused-vars` rule crashes on
  implicit reactive declarations, so it stays off for `.svelte`.
- `guard-nuia` (push stage, ~50 s): `svelte-check` with `frontend/tsconfig.nuia.json`
  (`noUncheckedIndexedAccess`), errors counted per file. Production files are at zero; `baselines/nuia.txt` holds
  only `*.test.ts` files until they are converted, after which the flag moves into the main tsconfig.
- `guard-duplicates` (push stage): `jscpd` (pinned) with `--min-tokens 100`; 50 flags import/kwarg boilerplate.
  `check_duplicates.py --report PATH` writes the full clone list.
- eslint `complexity` 15, `max-depth` 4 and `max-lines-per-function` 150 run at `warn`; existing offenders are in
  `baselines/eslint.txt`, new ones fail the ratchet.
- `guard-unused-css` (push stage, ~2 s): a class defined in `frontend/src/styles/*.css` or a component `:global(...)`
  selector must appear as a whole token in a non-test `.svelte`/`.ts`/`.js`/`.html` source (style blocks stripped).
  A template/concat prefix (`badge-${tone}`) covers its family; classes rendered by a dependency (highlight.js,
  the datepicker, the range slider) are `third-party-dom` allowlist entries. Lenient by design: it reports only
  certainly-dead classes.

## Running them all: `scripts/lane-check.sh`

One command for every FAST whole-tree guard (the ratchets above plus `guard-hidden-mypy`, `i18n-parity`,
`audit-tests` and the frontend `test:audit`), run in parallel at `nice 10`, one `PASS`/`FAIL` line each, exit 1 on
any failure. Each guard runs its hook's own `entry`, read from `.pre-commit-config.yaml` by
`scripts/lib/precommit_entry.py` (`language: python` hooks under `backend/venv/bin/python`, which holds the same
pins); a renamed hook makes it exit 2 rather than skip. `--only a,b`, `--skip a,b`, `--list`. It is not
pre-commit (no stash, safe beside other writers) and runs no builds, `bandit -r` or test suites. Lanes run it
before reporting and paste its output.
