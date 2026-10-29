# v0.6.0 release runbook (written 2026-10-28)

Companion to `v060_v052_completion_plan.md` (what to build). This file is what happens AFTER the code
lands: verification, release stages, and every public artifact. A fresh agent must be able to run it
standalone. Release date target: **Oct 31, 2026**. The release is cut ONLY through
`./scripts/release.sh` (never hand-run tag, docker push or gh release); `tag`, `publish`, `promote`
and `finish` need `--yes` and the owner's go.

## Preconditions (all must hold before Day 2)
1. Wave-1 PR (`fix/v060-wave1-integration` plus audit fixes, lane B) and the privacy PR
   (`feat/privacy-controls-1280` plus glue and docs) are merged to master by merge commits, each only
   after "Backend Tests (pytest)" shows PASSED.
2. An Opus read-only audit of EACH integration branch has run before its PR opened. Instruct it to
   demonstrate bugs: re-introduce the guard-removal in a `git archive` copy and see whether tests notice.
3. Gate 0c: `gh workflow run pre-commit.yml --ref master` on the final master head is fully green.
   Known flake #1305 is fixed by lane B first; a red result is investigated, never just re-run.

## Git flow (read before touching anything)
The scripts own tags, the release branch and the GitHub release; this section only says WHERE to run them.
Source of truth: `.claude/skills/release/SKILL.md`, `docs-site/docs/developer-guide/releasing.md`.
- **Never** hand-run `git tag`, `docker push`, `gh release create`, or edit `VERSION`, `pyproject.toml`,
  `package.json`, `package-lock.json` (use `release.sh bump`). No `--no-verify`, no force-push, no
  squash-merge; every merge into master is a merge commit via PR.
- Master is protected (4 required checks + a review; no force-push or deletion), so the bump commit
  cannot land on it directly. That is the ONLY reason a branch exists.
- Freeze: once both integration PRs and Gate 0c are done, nothing else merges to master until the tag.
- **Run the release from the MAIN checkout. There is no release worktree and no file copying.** The
  gitignored files `preflight` needs (`nginx/ssl/server.crt` + `server.key`,
  `scripts/release-tests/.env.test-secrets`) are already there. Steps, in order:
  1. Main checkout on `master`, fast-forwarded to the verified head (`git pull --ff-only`), clean
     `git status`, no other writer in the checkout and no other agent running pre-commit.
  2. Stop the stack (`./opentr.sh stop`). It is brought back up only for the `test` stage (step 5)
     and `rehearse --yes` restarts dev itself.
  3. Cut the short-lived branch: `git switch -c release/prep-0.6.0`.
  4. `./scripts/release.sh reset 0.6.0`, then `preflight`, `bump` (makes the
     `chore(release): bump version to 0.6.0` commit on the branch).
  5. CHANGELOG prose, blog, screenshots/GIF/baseline commits and docs fixes are further conventional
     commits on this branch (after `bump`, before `verify`). Then `verify`, `test` (dev stack UP),
     `build`, `scan`. The checkout must be CLEAN before `tag`.
  6. **Push the branch BEFORE `rehearse`**: `git push -u origin release/prep-0.6.0` (scenarios A and C
     download manifests from it; `rehearse` exits 3 otherwise). Then `rehearse --yes`.
  7. Each of `tag`, `publish`, `smoke` (stack stopped again), `promote`, `finish` is a separate go
     from the owner. Never chain them with `run --yes`.
- The image that is built, scanned and rehearsed must be the exact commit that is tagged (the About
  dialog shows the build SHA). So **tag the prep branch head**, never a later merge commit.
- `tag` pushes an ANNOTATED tag `v0.6.0` and cuts `release/0.6` from the tag (first minor ships
  there; it fails if the tag is not an ancestor of that branch). Check `git ls-remote origin
  release/0.6` afterwards.
- **Bring it back to master:** after `tag`, open a PR `release/prep-0.6.0` -> `master` (merge commit,
  never squash; Backend Tests PASSED), so master contains the bump, CHANGELOG and docs. Then verify
  `git merge-base --is-ancestor v0.6.0 origin/master` and switch the main checkout back to `master`
  (`git pull --ff-only`) and restart the dev stack. (0.5.1's bump rode a PR the same way.)
- Delete the merged prep branch and any merged lane branches/worktrees afterwards (`git worktree
  remove`, then `git branch -d`; never `-D` a branch whose work is unmerged).

## Docs audit (before the prose freeze)
README.md (1,369 lines), 80 docs-site pages, user-facing `docs/*.md`, `docs/RELEASE_PROCESS.md`,
`scripts/release-tests/README.md` and the 53 CLAUDE.md files may all be stale after the Oct 20 UI
overhaul and the 0.6.0 work. Six Sonnet auditors (README; install + getting-started + configuration;
features + user-guide; operations + authentication; developer guide + linked docs + release docs;
CLAUDE.md files) check every claim against the code (commands, paths, routes, env vars, ports, image
names, UI labels from `frontend/src/lib/i18n/locales/en.json`), fix what is wrong, and list gaps. They do
NOT edit CHANGELOG.md, the blog post, `upgrading-to-0-6-0.md` or privacy pages (other lanes own them).
Then `cd docs-site && npm run build` must pass with no broken links.

## Schedule
| Day | Work |
|---|---|
| Oct 28 | Finish lanes; audits; open and merge both PRs; Gate 0c |
| Oct 29 | Wave 3 on a fresh master stack (no writers): integration gate, e2e, #1306, #1308 (closes #1313), #1309, #1282 browser pass (light, dark, ar), transformers/GPU transcription check, v448 migration over populated data. Refresh screenshots and baselines. Fix findings |
| Oct 30 | Fixes from Wave 3; docs, README, blog, upgrade guide final; #1310 roadmap regen; `release.sh preflight`, `bump`, write CHANGELOG + blog, `verify`, `test` (dev stack must be UP), `build`, `scan` |
| Oct 31 | from the main checkout: push `release/prep-0.6.0` to origin, `rehearse --yes` (auto-stops and restarts dev; HuggingFace token is already in `scripts/release-tests/.env.test-secrets`), then with the owner's go: `tag`, `publish`, stop the stack again, `smoke`, `promote`, `finish`. Cloud handoff after |

If Wave 3 shows a release blocker, slipping the date is cheaper than shipping it. Cut list if time runs
out, in order: #1180 profiling (findings only), native-speaker review of translations, optional polish.

## Wave 3 checklist (fresh master stack, no other writers)
- `./opentr.sh start dev --fresh master`; `./scripts/run-dev-tests.sh --full` (or
  `./scripts/run-integration-tests.sh` plus `./scripts/e2e/run-e2e.sh`).
- Mind: other service answering on port 5178 gave MinIO `SignatureDoesNotMatch` in a lane's local run.
  Make sure the stack's MinIO is the one the tests probe.
- #1306 deletion-residue seeds and live-stack gate; #1308 notification flow with real Celery workers and
  a real upload (the Gender row must finish as skipped, then close #1313); #1309 reranker thread cap,
  `benchmark_rag` parity, MS Word and full-image DOCX checks.
- v448 migration against a populated database copy (restore a backup into a throwaway stack).
- transformers 5.10 / huggingface-hub 1.17 (lane B): one real GPU transcription with diarization,
  an NLP/embedding path and the reranker; build the prod backend image and run Trivy to confirm the
  five CVEs are gone. If blocked, `./scripts/release.sh scan 0.6.0 --force-scan "<CVE ids and why>"`
  is pre-approved by the owner.
- Built-image dependency checks (build the real prod CUDA image first; none of this was possible in
  CPU venvs): `torchaudio` loads without a missing-library error and the app's audio loader never imports torchcodec (a torchcodec that fails to import, e.g. a missing `libnppicc`, is reported by the rehearsal GPU check but is not a failure) (the repo pins
  torchcodec 0.11.1 from the cu128 index; the images that ran on staging had PyPI 0.17.0, a CUDA-13
  build the app never imports; keep whichever loads cleanly), one real GPU transcription with
  diarization, the NLP/embedding path, the reranker, the toxicity model and GLiNER really loading
  under transformers 5.10.4 / huggingface-hub 1.17.0 (the models-marked tests now FAIL, not skip, when
  weights are cached but cannot load), a real model download with progress and resume, and Trivy on the
  built image to confirm the five CVEs are gone. Run `scripts/check-requirements-resolve.py` once on
  the release-prep branch.
- Privacy behavior on real services: sidecar gender false, no docs written to `speakers_v4` when off,
  purge end to end, org panel gating.
- #1282 browser pass with screenshots: upload tray, notification card, search page, chat, About
  dialog and stale-build banner, export menu (txt, md, docx, pdf), legal modal, org collections,
  audit and holds panels, reranker status row, org and user privacy surfaces and the purge dialog.
  Check light, dark and Arabic (RTL). Fix findings in the same wave.

## Public artifacts (checklist)
- [ ] **Docs screenshots** in `docs-site/static/img/screenshots/` are from Aug 29, before the Oct 20 UI
      overhaul. Refresh them with the `docs-screenshots` skill from the live stack; commit.
- [ ] **README workflow GIF** `docs-site/static/img/opentranscribe-workflow.gif` (referenced at the top
      of README.md): re-record from the new UI. Re-read README feature list for the 0.6.0 themes
      (organizations, collections, audit and legal holds, privacy controls, export formats, pipeline
      composition, model library) and fix anything stale.
- [ ] **Visual baselines**: `release.sh verify` has a blocking `visual-baselines-fresh` criterion; last
      release used the recorded opt-out `SKIP_VISUAL_BASELINE_FRESHNESS=true`. Re-capture the committed
      `__screenshots__` baselines against the isolated seeded stack ONCE, after the UI is final, and
      commit. Do not waive it twice without the owner.
- [ ] **Blog post**: `docs-site/blog/2026-10-14-v0.6.0-release.md` is `draft: true`. Rename it to the
      real release date, remove the flag, update the content to match what actually shipped (privacy
      controls, audit and holds, exports, the notification stages, the transformers upgrade if kept).
- [ ] **Upgrade guide** `docs-site/docs/operations/upgrading-to-0-6-0.md`: migrations `v440`-`v449` (DONE; `v448` is
      the task one-active-row guard with a lock and a maintenance window) and the privacy section. Note
      `AWS_TRANSCRIBE_BUCKET` is now required for the AWS Transcribe provider (and
      `AWS_TRANSCRIBE_BUCKET_OWNER` when STS is unreachable).
- [ ] **CHANGELOG**: `[Unreleased]` becomes `[0.6.0] - <date>` via the `bump` stage (never by hand).
- [ ] **Roadmap** (#1310): `python3 scripts/generate-roadmap.py`; check the generated `/roadmap`
      page and `docs-site` build with no broken links.
- [ ] **Release notes / GitHub release**: written from the CHANGELOG by `release.sh`; read it before
      `publish`.
- [ ] **Docker Hub images**: capability is in the repository name (`opentranscribe-backend` CUDA,
      `opentranscribe-backend-lite` CPU); multi-arch needs `USE_REMOTE_BUILDER=true`; Trivy scan
      must pass or carry a recorded `--force-scan` reason. Check `./scripts/docker-build-push.sh
      list-platforms`.
- [ ] **Docs site deploy** happens through `deploy-docs.yml` after merge; confirm
      https://docs.opentranscribe.app shows the new pages (privacy controls, upgrade guide, blog).
- [ ] **Cloud handoff**: send the final master SHA to the cloud session; the owner types "go" there.
      The downstream list must include `AWS_TRANSCRIBE_BUCKET`, the new seam notes, and migrations
      v440-v449. Note the multi-tenant demo is affected by the tenant-scope fixes until it redeploys.

- [ ] **Security reports**: `security-reports/SECURITY-ADVISORY.md` is stamped "Applies to v0.5.1" and
      names nginx 1.29.8 (the frontend image is now nginx 1.31.2): refresh it for 0.6.0 from the
      `scan` stage output. The committed `*-dockle.json` reports show FATAL DKL-DI-0005 ("clear apt-get
      caches") on the backend and lite images; the release `scan` criteria do not gate on Dockle, so
      decide: fix the Dockerfile apt cleanup or record the finding in the advisory.
      `security-scan.sh` usage text omits `lite` and `blackwell`; `generate-roadmap.py`'s docstring still
      says a deleted CI job calls `--check`.
- [ ] **License/SBOM report** (`security-reports/license-compliance/`): no release stage regenerates it.
      Re-run `scripts/generate-sbom-report.py` against the 0.6.0 images (output dir is fixed on the
      docs-audit branch) so the report matches what ships.

## Release stage order
`preflight -> bump -> verify -> test -> build -> scan -> rehearse -> tag -> publish -> smoke -> promote -> finish`.
Write the CHANGELOG prose and the blog post right AFTER `bump` (it promotes `[Unreleased]` to
`[0.6.0] - <date>` and opens a fresh one) and BEFORE `verify` (which builds the docs site so a
duplicate blog slug is caught). Build uses `BUILD_MODE=local ./scripts/docker-build-push.sh` via the
stage, never a bare `docker build` (it would report version "unknown"). `tag` before `publish`;
`promote` and `finish` last. `scan` scans the local images.
Run `./scripts/release.sh reset 0.6.0` first (rehearsal history reads as current until reset; a
preflight run on Oct 28 already wrote a ledger entry).
`rehearse` refuses (exit 3) while the live stack is up. Exit codes: 0 pass, 1 gate failed, 2 misuse,
3 precondition unmet, 4 operator abort. A gate override is `--force-<stage> "reason"` and is recorded
as overridden, never as a pass.

## Stage preconditions the scripts enforce (verified against scripts/release/*.sh)
- `run X` WITHOUT `--yes` stops at `tag` with exit 4. `tag`, `publish`, `promote`, `finish` need `--yes`.
- `bump` also refreshes the roadmap snapshot and commits; `verify` runs the visual-baseline gate
  (`SKIP_VISUAL_BASELINE_FRESHNESS=true` records `waived`, not a pass); `preflight` checks the CHANGELOG.
- `test` needs the DEV STACK UP (it probes 5176) and runs `run-dev-tests.sh --backend-only --e2e-smoke
  --export-capability`. If that run is NOT MEASURED (exit 5) the stage maps it to exit 4
  ("aborted: nothing ran"), which is misleading: read the log, do not assume the gate ran.
- `rehearse` runs three scenarios (fresh install, upgrade, lite) and exits 3 unless `release/prep-0.6.0`
  is PUSHED to origin (scenarios A and C download manifests from it): `git push -u origin
  release/prep-0.6.0` first. The live stack must be down: either `./opentr.sh stop` yourself or pass
  `--yes` / `RELEASE_AUTO_STOP_STACK=true` so it stops the stack and restarts dev on exit.
- `smoke` also needs the stack stopped (exit 3 otherwise). Rehearse restarts dev on exit, so stop it
  again before `smoke`.
- `tag`: the "is an ancestor of origin/release/0.6" check only bites on a patch; the first cut creates
  the branch from the tag.
- `finish` refuses until `release-validate.yml` is green for the tag SHA.
- `blackwell` appears in `list-platforms` but is skipped by build and publish.

## Known risks
- Lane B's transformers 5.10 / huggingface-hub 1.17 move is a major-version jump with whisperx capping
  huggingface-hub<1.0 upstream. It must pass a real GPU transcription before it ships.
- v448 takes a table lock on `task`; run it in a maintenance window on large installs.
- Translations were written by agents without native review (ar, ja, ko, zh especially).
- Frontend vitest has 5s timeouts that fail under host load (47-58 on 48 cores); do not run gates
  while writers are active, and re-run in isolation before treating one as a defect.
