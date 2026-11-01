# File-length policy: measured limits by file type

Owner decision (2026-11-01): file length is a smoke alarm, not a design rule. The aim is only to
stop 2,000-line files nobody can read. A single-purpose file may stay long; a mixed-concern file
is split whatever its length. A number never justifies a split on its own, and never justifies
trimming code to fit.

Enforced by `scripts/guards/check_file_size.py` (pre-commit hook `guard-file-size`; tested by
`backend/tests/unit/test_guard_file_size.py`). Baseline of files still over a hard cap:
`scripts/guards/baselines/file_size.txt` (`path<TAB>lines<TAB>reason`; may only shrink).

## Limits

| What | Soft target (reported only) | Hard cap (fails unless baselined) |
|---|---|---|
| Python, TypeScript, JS, shell (non-test) | 500 lines | 1,000 |
| Tests (`tests/` trees, `test_*`, `conftest.py`, `*.test.*`, `*.spec.*`, `*_test`) | 1,000 | 1,500 |
| Svelte `<script>` logic (key `path::script`) | 400 | 600 |
| Svelte whole file (markup and CSS included) | 1,000 | 2,000 |
| Every `CLAUDE.md` (loaded into agent context) | 300 | 400 |

Out of scope: `backend/alembic/versions/` (migration history), `frontend/src/lib/i18n/locales/`
(translation data), `backend/openapi.json` (generated), `backend/mutants/` (generated), anything
outside `backend/`, `scripts/`, `frontend/src/`, `frontend/scripts/`, `docs-site/src/` and root `*.sh`.

Why Svelte is measured differently: a component's length is mostly markup and CSS, which says
nothing about mixed concerns. The `<script>` block is where logic lives, so that is capped
tightly; long stylesheets are solved by shared styles and components (design tokens, utility
classes), not by splitting a component.

## Measured distribution (2026-11-01, tracked files on `chore/repo-cleanup`)

| group | files | median | p90 | max | soft | hard | over soft | over hard |
|---|---|---|---|---|---|---|---|---|
| Python (non-test) | 1204 | 217 | 515 | 2731 | 500 | 1000 | 134 | 14 |
| TypeScript/JS (non-test) | 231 | 90 | 278 | 1508 | 500 | 1000 | 12 | 4 |
| Shell (non-test) | 118 | 248 | 898 | 4893 | 500 | 1000 | 26 | 11 |
| Tests (py/ts/js/sh) | 2188 | 191 | 520 | 2028 | 1000 | 1500 | 21 | 4 |
| Svelte <script> logic | 375 | 59 | 258 | 2355 | 400 | 600 | 15 | 11 |
| Svelte total file | 375 | 238 | 708 | 2876 | 1000 | 2000 | 20 | 2 |
| CLAUDE.md | 57 | 103 | 595 | 1215 | 300 | 400 | 12 | 10 |

Reading it: the typical file is small (medians 59 to 250 lines) and the 90th percentile of
non-test Python is 515, so a 1,000-line hard cap touches 14 Python files, not hundreds. Under the
old flat 700 cap the baseline held 179 entries; under these limits it holds 56 (the sum of the
"over hard" column). The worst offenders are Svelte (11 components with a 600+ line script;
`routes/files/[id]/+page.svelte` has a 2,355-line script), shell (`opentranscribe.sh` 4,893 lines,
11 files over 1,000) and the `CLAUDE.md` files (10 over 400).

Re-measure with the snippet in the git history of this file (it imports
`check_file_size.metrics_for` and `measure`); these numbers rot.

## How to apply the rule

1. Decide by cohesion first: does the file have more than one reason to change, hold unrelated
   sections, or share mutable state between unrelated jobs? Split it. If not, leave it.
2. Over a hard cap and cohesive: baseline it with a written reason (`cohesive: ...`), do not split.
3. Split along real seams (gates, I/O, providers, helpers, response building), move code verbatim,
   keep security reads (`ctx.bypass`, permission and tenant checks, audit calls) inside the handler,
   repoint every importer and every test patch target (patch the module whose caller reads the
   name), prove sorted test ids are unchanged.
4. Never raise a baseline for your own change. If a repoint import adds a line to an at-cap file,
   delete a comment that only restates the code. Moved debt from master is the one exception and is
   logged in the review file.
5. After a split, check cohesion again: re-merge a split that fragmented one idea, and make names
   imported across modules public (no leading underscore).
