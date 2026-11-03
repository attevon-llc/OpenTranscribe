# scripts/release-tests — release rehearsal harness

Moved intact from `scripts/CLAUDE.md`. The staged release orchestrator is `scripts/release/` (see `scripts/release/CLAUDE.md`); this directory is what its `rehearse` stage drives.

- **Release rehearsals** — `release-tests/`: `test-fresh-install.sh`, `test-upgrade.sh`
  (both auto-detect FROM/TO — see `lib/versions.sh`), and `test-lite-mode.sh` (no
  FROM/TO to detect — always rehearses the CURRENT `VERSION`'s `--lite` deployment shape
  against mocked cloud ASR + mocked LLM), with `lib/guardrails.sh` as the
  safety firewall and `lib/{compose-patch,api-client,assertions,versions,model-cache}.sh`.

  ⚠️ **Scenario D, `test-cold-model-download.sh`, is the ONLY thing that runs
  `download-models.py` from an empty cache, and `rehearse` runs it first.** v0.6.0 shipped a
  pyannote group that exits 1 for every user (CPU and GPU: synthetic warm-up audio, VAD finds no
  speech, transformers 5.10.4 indexes an empty batch) while A-C passed, because they install with
  `OPENTRANSCRIBE_UNATTENDED=1` (the installer then skips `download_ai_models`) and seed a warm
  cache. D runs every `DOWNLOAD_GROUPS` group (derived by AST, `lib/cold-model-download.py groups`)
  in its own container, CPU-only, against the image under test with this checkout's script
  mounted; `lib/cold-model-download.py verify` checks the weights independently (the downloader
  calls `partial`/`skipped` success), and a `cp -al` nltk leg requires a loud failure or a
  loadable corpus. A passing D exports `OT_COLD_CACHE_SEED` and Scenario A seeds from it. Needs
  `HUGGINGFACE_TOKEN` (env, or `OT_RELEASE_TEST_SECRETS`/`.env.test-secrets` — a worktree has no
  copy of that gitignored file, so point `OT_RELEASE_TEST_SECRETS` at the main checkout's);
  without it, or with `DOWNLOADER_IMAGE=`/`OT_RELEASE_TEST_ALLOW_STALE_IMAGE=`, it can fail but
  never pass. It replaced `scripts/test-model-download.sh` (`:latest`, `--gpus all`, wired into
  nothing), which was deleted. Tests: `backend/tests/unit/test_release_cold_model_download.py`.
  Do not "speed it up" by seeding the cache: an empty cache is the whole measurement. It
  refuses a non-empty root, bounds every container (`OT_COLD_CACHE_{GROUP,PROBE}_TIMEOUT_S`,
  `OT_COLD_CACHE_DEADLINE_S`; a deadline is rc 124 or missing, never a pass), hardlinks a COPY
  of nltk_data (never the cache A seeds from), and `--purge-cache` removes the weights with the
  image under test by ID. `rehearse` runs it BEFORE stopping the live stack and seeds A only when
  all three D criteria pass. CPU-only: a GPU-only downloader regression is not covered.

  **Where the rehearsal's wall clock actually goes, and why it is near its floor.** Measured on
  a green run: **~30 min total — fresh ~2 min, upgrade ~24 min, lite ~3 min.** So the upgrade
  scenario is ~80%, and it is two hops (`ver_upgrade_sources` derives `{v0.4.1, v0.3.3}` for a
  v0.5.0 TO). The obvious saving is to drop the second hop; **don't** — `lib/versions.sh`'s own
  header is the reason: v0.3.3 shipped only 2 Alembic revisions and bootstrapped its schema from
  `database/init_db.sql`, so it is the **only** source that exercises the pre-Alembic bootstrap
  path. The two rejected alternatives are recorded there too, one of which
  (`{v0.4.1, v0.4.0}`) buys a second hop whose Alembic chain is *identical* — two hops' price for
  one hop's coverage. A patch-level TO already collapses to one hop on its own.

  For **iteration** (not for a release), `OT_UPGRADE_SOURCE_MINORS=1` halves it — verified:

  ```bash
  ./scripts/release-tests/test-upgrade.sh --list-sources                     # v0.4.1, v0.3.3
  OT_UPGRADE_SOURCE_MINORS=1 ./scripts/release-tests/test-upgrade.sh --list-sources   # v0.4.1
  ```

  ⚠️ Never cut a release on a single-hop rehearsal: the hop you dropped is the one covering the
  schema shape least like today's. Roughly 38% of the remaining time is **real transcription**,
  which is the thing being rehearsed and is not overhead to remove.

  ⚠️ **The Docker Hub tag memo (`lib/versions.sh`'s `ver_hub_has`) lives in
  `${OT_HUB_CACHE_DIR:-$XDG_CACHE_HOME/opentranscribe}/hub-tags` and entries EXPIRE
  (`OT_HUB_CACHE_TTL_S`, default 6 h).** It used to be `${TEST_ROOT:-${TMPDIR:-/tmp}}/.hub-tags`,
  wrong in both directions at once: `TEST_ROOT` is a per-run timestamped directory, so a
  rehearsal re-probed every tag and the memo saved nothing in the case it exists for; and with
  `TEST_ROOT` unset (`release.sh status`, `90-promote.sh`) it fell back to a bare `/tmp` file
  that **never expired**, so a cached "no" for a tag published five minutes later stayed "no"
  forever — which makes a real published release invisible to the tooling that decides what to
  rehearse against. Entries are `<image> <yes|no> <epoch>`; a stampless (pre-TTL) entry is
  treated as expired, not fresh. `backend/tests/unit/test_hub_tag_cache_ttl.py` sources the real
  function against a fake `docker` and counts probes — a grep for the TTL variable would pass
  against a version that defines it and never reads it.

  ⚠️ **NEVER hardlink `nltk_data` when seeding the model cache — seed through
  `lib/model-cache.sh`.** Both scenarios used `rsync -a --link-dest=<src> <src> <dst>`, which
  hardlinks every file. That is correct and free for the HuggingFace/torch/sentence-transformers
  trees and **fatal** for `nltk_data`: nltk >=3.10's pathsec hardening (CWE-59) refuses any
  corpus file with `st_nlink > 1`, so all 130 files were poisoned — the shared cache plus one
  link per test run — and **every transcription in both scenarios failed**. It is why the ledger
  read `rehearse=failed`, which was mistaken for staleness. `mc_seed_cache` hardlinks the big
  trees, real-copies the pathsec-sensitive one, and `mc_assert_no_hardlinks` fails at SEED time
  naming the cause; `MC_PATHSEC_SUBDIRS` is a list so a second hardened library joins nltk there
  rather than forking the code. `.seeded-from-live` means "seeded", not "seeded correctly", so a
  cache built by an older revision is repaired on reuse. Verified by
  `backend/tests/unit/test_release_model_cache_pathsec.py`, which builds real hardlinked trees —
  a grep would pass against a helper that does nothing. The app-side half of this (an unreadable
  corpus must degrade, not fail the job) is in `backend/app/utils/CLAUDE.md`.

  ⚠️ **A pipeline failure must dump diagnostics BEFORE teardown.** The report used to say only
  `file <uuid> ended in status=error`; the real cause lived in `media_file.last_error_message`
  and the worker logs, both destroyed by the cleanup that followed. That cost a full re-run to
  learn anything, and made a harness bug look like a product bug. `ac_dump_failure_diagnostics`
  (`lib/api-client.sh`) prints every error-ish field of the file record — located **by name**,
  since that column has been renamed once already — and tails each pipeline worker.

  ⚠️ **A bare non-fatal-by-design helper call under `set -euo pipefail` silently truncates every
  phase after it, with no error trace.** `dbs_diff_fingerprints()` (`lib/db-snapshot.sh`) is
  documented as informational — its non-zero return isn't meant to be fatal, `as_record` has
  already logged the PASS/FAIL either way — but `test-upgrade.sh` called it bare (unwrapped in
  `if ... ; then`) at two sites. The first digest mismatch tripped `set -e` and killed the script
  mid-phase-15: phases 16-18 (three phases, 8+ assertions, including the only checks that prove a
  rollback actually serves a real user's data) never ran, and all that surfaced was a bare exit 1
  (#617). Fixing that exposed the identical shape one phase later — an unguarded `curl` against
  the frontend with no readiness wait (unlike the backend's `ac_wait_for_health`), which died the
  instant the frontend wasn't yet reachable and truncated phases 17-18 the same way (#618). Both
  were diagnosed from `$TEST_ROOT/.phase/`: the absence of a `NN.done` marker is what proved which
  phase never ran, since the log itself gave no clue. When adding a new assertion, intermediate
  helper call, or `curl` to `test-fresh-install.sh`/`test-upgrade.sh`, wrap anything whose failure
  is meant to be recorded-not-fatal in `if ... ; then ... fi` — the pattern
  `selftest-rollback-fault-injection.sh` already uses correctly — rather than trusting a bare
  non-zero exit to fail loudly; under this harness's default `set -e` it fails silently instead.

- **Harness self-test** — `release-tests/selftest-cleanup.sh` (25 cases). Run it
  after ANY change to `lib/guardrails.sh`: it exercises the code that deletes
  volumes, and caught the live-data marker check deleting a volume it should have
  refused (the marker was read from the host, where the root-owned mountpoint made
  every volume look unmarked). Cases 8-10 (issue #598) cover the rollback tail's
  additional guardrails (`gr_assert_target_is_test_database`,
  `gr_fingerprint_repo_backups`/`gr_assert_repo_backups_untouched`,
  `gr_assert_not_repo_cwd`) — the tail runs `DROP DATABASE`, which none of the
  earlier cases needed to guard against.
  `release-tests/selftest-rollback-fault-injection.sh` (6 cases, ~1 minute) is the
  sibling for `lib/db-snapshot.sh`: it proves `test-upgrade.sh`'s rollback-tail
  assertions can actually FAIL (a truncated dump, a stale comparison oracle, a
  skipped damage step) against a throwaway isolated Postgres container, never the
  4-hour scenario. Run it after any change to `lib/db-snapshot.sh` or to
  `test-upgrade.sh`'s phases 13-17. It also pins a real, measured finding: a
  plain-format `pg_dump` truncated mid-`COPY` replays with exit 0 and silently
  wrong data — psql treats an unterminated `COPY ... FROM stdin` at EOF as simply
  ending the copy, not a parse error — so that fault mode's rehearsal assertion
  is a content digest, never `restore`'s exit code.
