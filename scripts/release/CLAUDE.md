# scripts/release — the staged release orchestrator

Moved intact from `scripts/CLAUDE.md`. Entry point: `scripts/release.sh`. Operator guide: `docs-site/docs/developer-guide/releasing.md`; agent interface: `.claude/skills/release/SKILL.md`.

- **Cutting a release** — `release.sh` is the ONLY entry point; never hand-run
  `git tag` / `docker push` / `gh release`. 12 stages in `release/NN-<stage>.sh`,
  each independently runnable (`--skip`, `--only`, `--from`, `--dry-run`), with a
  ledger in `.release/<version>/steps/`. `tag`/`publish`/`promote`/`finish` reach
  outside the repo and need `--yes` plus their `ask` rule. Exit codes: 0 pass,
  1 gate, 2 misuse, 3 precondition, 4 abort. `--force-<stage> "reason"` overrides
  a gate with a MANDATORY reason, recorded as `overridden` (not a pass).
  `reset <version>` clears rehearsal history — do it before a real run, or the
  status table reports stale state as current.
  Full guide: `docs-site/docs/developer-guide/releasing.md`.
  - ⚠️ **Every stage's gates are DECLARED in `release/release-criteria.yaml` and RECORDED by
    the stage script, bidirectionally — never one without the other.** `release/criteria-lib.sh`
    exits **2** on either half breaking: an id recorded that the file does not declare, or an id
    declared that the script never checked (`criteria_assert_all_checked`). That 2 is
    pipeline-misuse, deliberately distinct from a gate failure — so it never gets read as a
    statement about the release. `10-preflight.sh`/`30-verify.sh` predate the library and carry
    their own inline copies; anything else must source it. The severity (`blocking`/`warn`) is the
    **file's** decision, so `record` accepts only `pass|fail|not-measured` — passing `warn` as an
    outcome is exit 2, because a caller choosing its own severity is how the two drifted before.
  - **Stock-port ownership and same-commit evidence.** `lib/stock-port-owners.sh` asks docker which
    container publishes 5173-5180 and for which compose project, so `rehearse`/`smoke` exit **3**
    (`live-stack-stopped` fail, naming the project and its stop command) for an `otfresh-*` stack too —
    the old label check saw only `opentranscribe`/`transcribe-app` and the scenarios then died in
    `gr_check_ports_free` as a scenario FAIL (exit 1). `preflight` warns on the same evidence. And
    `70-tag.sh`'s `artifacts-match-tagged-commit` refuses to tag unless the ledger's `test`, `build`,
    `scan`, `rehearse` are `done`/`overridden` with a sha that resolves to HEAD (ledger sha is
    short; compared as resolved commits). Both are tested by extracting their `BEGIN/END` blocks
    (`test_stock_port_owners.py`, `test_tag_artifacts_match_commit.py`).
  - **`not-measured` is not a pass and not a failure.** Against a blocking criterion it stops the
    stage exactly as a failure does; a fifth `waived` argument downgrades it to a warning, and
    that is for an EXPLICIT operator opt-out only (`NO_COMMIT=true`, `SKIP_CI_CHECK=true`) — never
    for a check that merely could not run. Same rule as `security-scan.sh`'s 1-vs-2 (#681).
  - **Adding a criterion does NOT change a stage's exit code, and must not.** Every gate keeps its
    existing early `exit N` where it was; `record` is called at the check site and the criteria
    emitted through a per-stage `fail_out` that exits the ORIGINAL code.
    `criteria_assert_all_checked` is reachable only on the fall-through path — on an early exit
    the later criteria genuinely were not checked, and the library's exit 2 would rewrite a gate
    failure (1) or an unmet precondition (3) into misuse.
  - `backend/tests/unit/test_release_criteria_wiring.py` asserts the same contract **statically**,
    because most stages cannot run on a developer machine (they need multi-GB images, a pushed
    tag, or Docker Hub credentials) and a mis-wired criterion would otherwise surface during a
    real release. It matches a **literal** `record <id>`, so a helper that calls
    `record "$criterion"` is invisible to it — `80-publish.sh`'s `check_verdict` returns the
    verdict for the call site to record, rather than recording it itself, for exactly that reason.

- **Install-path gate** — `verify-install-paths.sh` (issue #683). The ONLY check that compares
  the scripts at the TIP against the releases actually PUBLISHED. Everything else in
  `release-validate.yml` validates a tag against its own checkout, which structurally cannot see
  the failure this exists for: a self-hosted install has two sources and only one is pinned —
  `setup-opentranscribe.sh` comes from the default branch (the docs one-liner hardcodes it),
  everything it downloads comes from the resolved release tag. The installer is therefore always
  NEWER than what it installs. `release-manifest.txt` was added after v0.4.1 shipped, the new
  installer asked that tag for a file it had never heard of, and **every `curl | bash` install
  died for 22 days**. The manifest-fetchable step that could have caught it was gated
  `if: github.ref_type == 'tag'`, and neither installer script was in any `paths:` filter, so
  the PR that broke it ran this workflow zero times. Both are fixed.
  It runs the REAL shell functions out of the REAL scripts against the REAL GitHub in a temp dir
  — never a re-implementation of the download loop, which would have passed throughout the
  outage. Four paths: fresh install, `update-full` on a pinned install, `update-full` on a
  pre-pinning install, and the default one-liner's release resolution. **The pin check is the
  load-bearing one**: it compares each downloaded artifact against `git show <tag>:<path>`,
  because a fallback that also pulled *artifacts* from the tip would satisfy an
  existence-only assertion while silently shipping an unpinned install. Needs
  `fetch-depth: 0` for that reason. `MIN_SUPPORTED_RELEASE` (v0.3.0) is a declared floor with a
  written reason — v0.1.0–v0.2.1 predate two non-optional manifest paths; marking those
  `optional` to make old tags pass would stop the gate noticing if a CURRENT release lost them.
  Never lower it to silence a red run. Runs on PRs (latest release only), and on a **schedule**
  because publishing a release can break the invariant with no commit at all. The hermetic half
  (no network, so it runs in the fast suite and CI) is
  `backend/tests/unit/test_install_upgrade_scripts.py`'s install-path section.

### `80-publish.sh` pushes the BUILT legs; it never builds (audit P1-3)

`40-build.sh` writes `.release/<v>/legs.tsv` (`component, repo, platform, leg tag, local image
ID`; block `leg-image-ids`, criterion `leg-image-ids-recorded`, which also reads the
architecture back). `80-publish.sh` (block `push-built-legs`) then: (1) requires every
non-blackwell leg of `list-platforms` in that file and its CURRENT local ID equal to the recorded
one, for ALL legs before ANY push (`built-legs-match-ledger`; missing file = exit 3
not-measured, mismatch = exit 1 naming the leg); (2) `docker push <leg tag>` per leg, parsing
the digest the push reports; (3) `imagetools create --tag repo:vX.Y.Z repo@<pushed digests>`
(digests, not tags) -- never `:latest`, `promote` does that; (4) reads the registry back:
leg tag digest and the index entry for the platform must equal the pushed digest
(`registry-digest-matches-pushed`), and the pushed manifest's config digest must equal the
recorded ID (`registry-leg-matches-built-image`). No `docker-build-push.sh` build, no
`--no-cache`, no re-scan (scan already examined these image IDs). Tests:
`unit/test_publish_pushes_built_legs.py` (fake docker + fake registry, markers `BEGIN/END`).

⚠️ **What a push preserves.** The host daemon here is classic overlay2 (27.x). Remote-builder
arm64 legs are `--load`ed into it as images with a foreign architecture; they push fine, but the
manifest is regenerated at push time (layers may be recompressed), so the registry manifest
digest is NOT BuildKit's. The image config survives, and `docker image inspect .Id` is the
config digest, hence the config comparison; a containerd-store daemon preserves the manifest
and the manifest digest equals `.Id` (both accepted). `--load` also drops provenance
attestations (check (a) already falls back to the config blob). **Unverified until a real
publish:** a real `docker push` of a loaded arm64 image from this daemon. `remote-builder-available`
is still checked although publish no longer uses the builder. A rebuild between `scan`/`rehearse`
and `publish` re-records new IDs, so publish cannot detect that those were not re-run.

### `80-publish.sh`'s manifest gate: two document shapes, three exit codes

⚠️ **`imagetools inspect --raw` returns two different documents and they carry different
facts.** Measured against the published `opentranscribe-backend:v0.4.1`:

| ref | shape | declares platforms? | has layers? |
|---|---|---|---|
| `repo:tag` (published tag) | INDEX — `manifests[]` | ✅ (plus `unknown/unknown` attestations) | ❌ — per-entry `size` is the **~2.4 KB manifest blob** |
| `repo@<digest>` | MANIFEST — `config`+`layers` | ❌ (architecture lives in the config blob) | ✅ |

So the platform-set checks read the **index** and the size/layer comparison must read the
**per-platform manifest** — `resolve-digest` pulls the digest out of the index and the stage
re-inspects `repo@<digest>`. Feeding the index straight to `check-ratio` reports
`sizes a=0 b=0 ratio=inf` and fails **every time, even comparing an index against itself**;
that is how the gate shipped for its first revision, so it would have blocked the very
release it exists to protect. The equivalence check is keyed on **how many platforms the
index declares**, never on how many capability leg tags exist — gating on legs silently
exempted `frontend`/`docs`, whose 1.25 bound `releasing.md` documents.

**`scripts/lib/manifest_platform_check.py` exits 0 / 1 / 3**, and the split is the same rule
as `security-scan.sh`'s 1-vs-2 (#681): `1` = checked and WRONG, `3` = **could not check**
(malformed/empty/absent JSON, or a correct document of the wrong shape). Both fail the
stage; only `1` is a claim about the artifact. An uncaught `JSONDecodeError` would have
exited 1 and read, in the log, exactly like a real platform mismatch.

`85-smoke.sh` asserts CAPABILITY (`torch.version.cuda` empty for lite, non-empty for full),
and **must capture the `docker run` exit status**: an image that cannot execute at all prints
an empty string too, so discarding rc turns "could not check" into "CPU-only confirmed" — the
#680 failure mode with a green tick, in the stage whose job is to catch it.

### Cutting a release — `./scripts/release.sh`, never by hand

**Do not hand-run git tag / docker push / gh release.** Everything mechanical is a
stage in `scripts/release/NN-<stage>.sh`; `scripts/release.sh` owns arg parsing,
the ledger, and dispatch.

```bash
./scripts/release.sh status                 # where am I? (ledger table)
./scripts/release.sh explain publish        # preconditions, side effects, reversibility
./scripts/release.sh reset 0.5.0            # clear rehearsal history before a REAL run
./scripts/release.sh preflight 0.5.0        # seconds — fails fast
./scripts/release.sh run 0.5.0              # the whole sequence
./scripts/release.sh run 0.5.0 --dry-run    # print every command, execute nothing
./scripts/release.sh scan 0.5.0 --force-scan "reason"   # override, recorded
```

Stage order (each independently runnable, skippable with `--skip`, resumable with
`--from`):

```
preflight → bump → verify → test → build → scan → rehearse
          → tag → publish → smoke → promote → finish
```

- **`tag` / `publish` / `promote` / `finish` reach outside this repo.** Each needs
  `--yes` **and** its `ask` rule in `.claude/settings.json`. They are the only
  stages that touch Docker Hub or GitHub.
- **Exit codes are stable** — `0` pass, `1` gate failed, `2` misuse, `3`
  precondition unmet (live stack up, builder unreachable), `4` operator abort.
  `--json` on any stage emits a `criteria[]` and a `next[]` of legal moves.
- **Gates are overridable, never silently.** `--force-<stage> "reason"` requires a
  reason (there is no bare `--force`) and records `overridden` + operator in the
  ledger — not a pass.
- **Ledger** lives in `.release/<version>/steps/` (gitignored). Rehearsal history
  reads as current until you `reset`, so reset before a real run.
- `rehearse` runs **four** scenarios: **D** the model downloader from an empty cache, CPU-only,
  in the built image (the only gate that runs `download-models.py`; v0.6.0 shipped a pyannote
  group that failed for every user while A-C passed, `scripts/CLAUDE.md`), then fresh install,
  upgrade, lite. It **requires the live
  stack stopped**: without approval it refuses (exit 3) and prints the command; with `--yes` or
  `RELEASE_AUTO_STOP_STACK=true` it runs `./opentr.sh stop`, rehearses, and restarts dev from a
  trap. It also needs the release branch pushed to origin. `smoke` needs the stack stopped too,
  and `test` needs the dev stack UP (`docs/design/v060_release_runbook.md` has the sequence).
  `test-upgrade.sh` now runs to completion across all 18 phases, including the
  backup/rollback tail — previously masked by a `set -e` harness bug that
  silently truncated the script at the first non-fatal-by-design digest mismatch
  (`scripts/CLAUDE.md`'s rehearsal gotchas). That is not a claim the rehearsal
  always passes. The gap this used to name (#619) was **closed** 2026-08-28, but
  its fix is a **best-effort settle, not a guarantee**: phases 15/17 wait for each
  table's content digest to stop moving rather than tracking every async writer by
  name, so a slow enough write can still land after the synchronization point. See
  `full-test-matrix.md`'s coverage table for what each leg does and does not prove.
- Running `test-upgrade.sh`/`test-fresh-install.sh` directly (not through
  `rehearse`) in a backgrounded or non-interactive shell needs `--yes` — the
  `I UNDERSTAND` confirmation prompt has no tty to read from and fails with
  `No such device or address` otherwise.

Full guide: `docs-site/docs/developer-guide/releasing.md` (Developer Guide →
Releasing). Agent interface: `.claude/skills/release/SKILL.md`. Gate definitions:
`scripts/release/release-criteria.yaml`.

**Version facts are DERIVED, never recorded.** The Alembic head comes from the
`down_revision` graph (`scripts/release-tests/lib/alembic-head.py`), and FROM/TO
from the `VERSION` file + Docker Hub. Never add a checked-in table of versions —
the last one (`expected-schemas.tsv`) went stale because it was hand-maintained
and read by nothing.
