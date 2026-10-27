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

## Schedule
| Day | Work |
|---|---|
| Oct 28 | Finish lanes; audits; open and merge both PRs; Gate 0c |
| Oct 29 | Wave 3 on a fresh master stack (no writers): integration gate, e2e, #1306, #1308 (closes #1313), #1309, #1282 browser pass (light, dark, ar), transformers/GPU transcription check, v448 migration over populated data. Refresh screenshots and baselines. Fix findings |
| Oct 30 | Fixes from Wave 3; docs, README, blog, upgrade guide final; #1310 roadmap regen; stop the live stack; `release.sh preflight`, `bump`, `verify`, `test`, `build`, `scan` |
| Oct 31 | `rehearse` (live stack stopped; HuggingFace token is already in `scripts/release-tests/.env.test-secrets`), then with the owner's go: `tag`, `publish`, `smoke`, `promote`, `finish`. Cloud handoff after |

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
- [ ] **Upgrade guide** `docs-site/docs/operations/upgrading-to-0-6-0.md`: add migration `v448`
      (task one-active-row guard, lock, maintenance window) and the privacy section. Note
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
      v440-v448. Note the multi-tenant demo is affected by the tenant-scope fixes until it redeploys.

## Release stage order
`preflight -> bump -> verify -> test -> build -> scan -> rehearse -> tag -> publish -> smoke -> promote -> finish`.
Run `./scripts/release.sh reset 0.6.0` first (rehearsal history reads as current until reset).
`rehearse` refuses (exit 3) while the live stack is up. Exit codes: 0 pass, 1 gate failed, 2 misuse,
3 precondition unmet, 4 operator abort. A gate override is `--force-<stage> "reason"` and is recorded
as overridden, never as a pass.

## Known risks
- Lane B's transformers 5.10 / huggingface-hub 1.17 move is a major-version jump with whisperx capping
  huggingface-hub<1.0 upstream. It must pass a real GPU transcription before it ships.
- v448 takes a table lock on `task`; run it in a maintenance window on large installs.
- Translations were written by agents without native review (ar, ja, ko, zh especially).
- Frontend vitest has 5s timeouts that fail under host load (47-58 on 48 cores); do not run gates
  while writers are active, and re-run in isolation before treating one as a defect.
