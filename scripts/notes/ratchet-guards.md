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
- `guard-duplicates` (push stage): `jscpd` (pinned) with `--min-tokens 100`; 50 flags import/kwarg boilerplate.
  `check_duplicates.py --report PATH` writes the full clone list.
- eslint `complexity` 15, `max-depth` 4 and `max-lines-per-function` 150 run at `warn`; existing offenders are in
  `baselines/eslint.txt`, new ones fail the ratchet.
