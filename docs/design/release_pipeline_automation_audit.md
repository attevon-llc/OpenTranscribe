# Release pipeline automation audit (v0.6.0) — 2026-10-30

Audit of `scripts/release.sh` + `scripts/release/*.sh`, `run-dev-tests.sh`,
`run-integration-tests.sh`, `scripts/lib/dev-test-overlays.sh`, `scripts/lib/compose-project.sh`,
the corpus injector, and the rehearsal harness. This was a read-only pass: no code, stack or ledger
was changed. The evidence is this week's logs in `~/ot-cleanup-work/`
(`release-test{,2,3}.log`, `release-build*.log`, `release-scan*.log`) plus
`.release/v0.6.0/steps/`. **`inject-qmsum.log` does not exist** in `~/ot-cleanup-work/` or `/tmp`,
so nothing here relies on it.

A fresh implementation agent can execute section 7 without any other context. Sections 1-6 give
the reasons.

---

## 0. TL;DR

1. **The 13-skip failure is a test-design defect, not an environment gap.**
   `test_fusion_strategy_switch.py` (6) and `test_rag_eval_harness.py` (3) assert on whatever corpus
   the target deployment happens to hold. That breaks this repo's own hard rule
   (`.claude/skills/gate-hardening/SKILL.md:72-75`, `backend/tests/CLAUDE.md` "A gate test may not
   depend on the dev deployment's DATA"). The repo already ships the fix pattern, in two forms:
   a throwaway index (`test_search_completeness_opensearch.py:203-262`) and a minted-seed
   injection followed by `purge_injected` (`test_corpus_injection_synthetic_e2e.py:103-130`). Make
   both tests self-seed. After that **no injector guard has to be weakened.**
2. **`OPENSEARCH_TEST_MODEL_ID` is set by nothing in the repo**, so the 4 hybrid completeness tests
   (added 2026-09-29) have skipped on **every** gate run since they landed. The gate can resolve the
   id from the stack's own backend (`ml_model_service.get_active_model_id()`), the same way it
   already resolves Redis (`run-integration-tests.sh:550-564`).
3. **The skip ceiling of 7 is not calibrated.** It was derived on 2026-09-07 as 4 fusion + 3
   rag-eval, all corpus-dependent (`run-integration-tests.sh:207-219`). On a corpus-rich dev stack
   today it passes only by coincidence: 3 rag-eval + 4 hybrid = 7, a different seven from the
   documented ones. On a fresh stack the count is 6 + 3 + 4 = 13. Once (1) and (2) land the ceiling
   should be **0**, which `backend/tests/CLAUDE.md` already prescribes ("drops to zero, not to 8").
   The unit ceiling is also stale: it is set to **151** and the measured count is **82**
   (`release-test3.log:1859`), which leaves 69 silent skips of headroom.
4. **Rehearse and smoke will misfire after a test run on `otfresh-master`.** Their live-stack checks
   only look at the `opentranscribe` and `transcribe-app` compose projects
   (`65-rehearse.sh:74-77`; `85-smoke.sh:215` looks at `opentranscribe` alone). A fresh stack holding
   the stock ports passes that check. All three scenarios then die in `gr_check_ports_free` with
   exit 1, which is recorded as a release **FAIL** rather than an unmet precondition.
5. **"Built = scanned = rehearsed = tagged = published" is broken by construction in three
   places.**
   - The scan writes git-tracked files (`50-scan.sh:23` defaults to `./security-reports`), which
     forces a commit after the build. That moves HEAD past the build's baked `GIT_SHA`
     (`docker-build-push.sh:637`).
   - `tag` never compares HEAD with the ledger's build/scan/rehearse SHAs.
   - `publish` rebuilds everything from scratch and re-scans it (`80-publish.sh:77-79`;
     `BUILD_CACHE_REGISTRY` defaults to false). The docs claim validated bytes reach Docker Hub
     (`releasing.md:130`, `80-publish.sh:4-5`). That is false: only `:latest` equals `:vX.Y.Z`.
6. **Late discovery is structural.** Every stage checks its own preconditions only when it starts.
   Several of this week's failures would have surfaced at T+5 min from checks that take seconds:
   mutation evidence (`--check-baseline`), the corpus/model preconditions, the branch push,
   port/GPU ownership, Docker Hub and gh credentials, and an aarch64 resolve of the lite
   requirements.

The **P0 list (section 5)** is about 1.5-2 days of work. It is all test, script and gate code, and
none of it changes image contents. It is enough for `release.sh test → rehearse → … → finish` to run
without hand-fixing.

---

## 1. Evidence: this week's three `test` runs (each about 1 h)

| Run | Stack | Result | Root cause | Discoverable earlier? |
|---|---|---|---|---|
| `release-test.log` | otfresh-master | Unit/API: **106 failed** (`:5387`), Tier-A sweep FAILED (`:93`), integration ran red | env/stack state (Redis `localhost:6379` refused at `:3626`, sweep failure) | yes: a stack readiness probe |
| `release-test2.log` | otfresh-master | Unit: 1 failed + 2 errors (`:2286`); integration: 1 error + **13 skips** (`:2925`); mutation ratchet **NOT MEASURED 5/6** (`:3257`) | test bugs fixed by `f29f641e8` and others; mutation evidence stale after test edits | mutation: yes, via `--check-baseline` (seconds) |
| `release-test3.log` | otfresh-master | Unit: 1 failed (billiard probe 90 s timeout under load, `:449`); integration **NOT MEASURED 13 > 7** (`:2141`) | load-sensitive timeout (fixed in `da501e8bd`); corpus + model id | corpus/model: yes (seconds) |

Phase wall clock in run 3 (`:1859`, `:2140`, `:2360`): unit 944 s, FIPS 70 s, **integration
1978 s (33 min, serial)**, GPU 151 s, total backend phase 3567 s. `scripts/CLAUDE.md` still says
the integration phase takes "~9 min". That figure is stale.

Ledger, `.release/v0.6.0/steps/`:

| Stage | Status | SHA |
|---|---|---|
| preflight | done | 67f1e69a6 |
| bump | done | 6babe0439 |
| verify | done | 2af0c7878 |
| build | done | cc81ca19b |
| scan | **overridden** (libxml2 CVE-2026-6653) | cc81ca19b |
| test | failed | 5f5d4f458 |

HEAD is `da501e8bd`. **No two passing stages share a SHA, and nothing in the pipeline notices.**
The release branch `release/prep-0.6.0` has **no upstream** and is 29 commits ahead of
`origin/master`, so no GitHub CI has run on any of those commits (`pre-commit.yml` triggers only on
`pull_request` and `workflow_dispatch`).

Other runs this week:
- Four builds and five scans on 2026-10-29.
- The build stage passed while a lite arm64 leg failed (fixed in `ba36b99b0`).
- The Alpine apk cache was frozen (fixed in `cc81ca19b`).

Both were found at scan time, roughly 1-2 h after the cause existed.

---

## 2. Q1: what each stage and phase requires, and who provides it

Legend: **A** = provisioned automatically, **H** = needs a human or agent today, **—** = not
needed.

### 2a. `release.sh` stages

| Stage | Stack state | Data / corpus | Models / credentials | GPU | Ports | Evidence / other | Auto vs manual |
|---|---|---|---|---|---|---|---|
| preflight (`10-preflight.sh`) | any (stack-up is only a warn, `:325-335`, yaml `:129-131`) | — | PyYAML; trivy/grype/syft (warn); HF token in `scripts/release-tests/.env.test-secrets` (warn, `:252-259`) | — | — | remote builder `opentranscribe-multiarch` (blocking, `:215-234`); test media under `/mnt/nvm/opentranscribe-test-runs/test-media` (warn); ≥60 GB disk (warn); clean worktree | A checks, H fixes. **Does not check:** branch pushed, Docker Hub login, `gh auth`, GPU free, ports owned by `otfresh-*`, mutation evidence, visual freshness, test-stack readiness |
| bump (`20-bump.sh`) | — | — | `gh` for the roadmap (warn), npm | — | — | commits | A |
| verify (`30-verify.sh`) | — | — | — | — | — | `docs-site/node_modules` (H: `npm --prefix docs-site ci`); visual baselines newer than `frontend/src` (`:197-224`, blocking; recapture is H via `scripts/e2e/update-visual-baselines.sh` plus a seeded stack) | A except recapture |
| test (`60-test.sh`) | **UP**, probed only at `127.0.0.1:5176` (`:27`). An otfresh stack must run on the **default ports**, because the gate hardcodes 5173/5174/5176/5178/5180 (`run-integration-tests.sh:93,102,108`; `run-dev-tests.sh:314,355`) | **QMSum injection + a ≥10-chunk vocabulary corpus (H, from the NAS)** | stack HF token (export capability); **`OPENSEARCH_TEST_MODEL_ID` (nothing sets it)**; mock-llm and mock-asr (A, overlays); Redis creds (A, `run-integration-tests.sh:550-564`) | GPU 1 for the stack; host CUDA for `-m gpu` | stock | **`.mutation/<module>.{log,meta,testhash}` matching the current source and test hashes (H: 35-90 min lanes)**; `COMPOSE_PROJECT_NAME=otfresh-<name>` **must be exported** (H), because `compose_project_name` only auto-detects `opentranscribe`/`transcribe-app` (`compose-project.sh:44-49,75-104`) | mixed (see 2b) |
| build (`40-build.sh`) | any | — | remote builder for non-host arches (A, `:71-72`) | — | — | disk; one leg at a time (`:62-100`) | A |
| scan (`50-scan.sh`) | any | — | scanners; local leg images | — | — | writes to `${OT_SCAN_DIR:-./security-reports}` (`:23`), which is **git-tracked** for `*.txt`/`*.json` other than SBOMs; FAIL_ON_CRITICAL=true, so the known libxml2 finding needs **`--force-scan "<reason>"` (H) every release** | A + H override |
| rehearse (`65-rehearse.sh`) | **DOWN**: no `opentranscribe`/`transcribe-app` containers (auto-stop with `--yes`, `:145-198`); guardrails also refuse any `opentranscribe-*` container or `opentranscribe_*` volume | release-test media | HF token in the test-secrets file; Docker Hub images for the FROM versions (for TO=v0.6.0 that is **v0.5.1 and v0.4.1 only**: `lib/versions.sh:251-311`, two minors, so the pre-Alembic v0.3.3 hop is gone) | **GPU 1** (`TEST_GPU_DEVICE_ID=1`, `test-fresh-install.sh:67`, `test-upgrade.sh:137`) | **5173-5180 free** (`gr_check_ports_free`, `lib/guardrails.sh:333-351`, exits **1**) | **branch pushed to origin** (`:309-318`, H); a local `davidamacey/*:vX.Y.Z` image is reused if present **without checking its revision** (`test-fresh-install.sh:249-263`) | H: push, free ports/GPU (an otfresh stack is not detected) |
| tag (`70-tag.sh`) | — | — | git push credentials | — | — | clean worktree (`:42-47`), `[X.Y.Z]` CHANGELOG section, `--yes` (`release.sh:224-230`). **No check that HEAD equals the built/scanned/rehearsed SHA** | H go |
| publish (`80-publish.sh`) | — | — | remote builder; **Docker Hub push credentials (never preflighted)** | — | — | **full rebuild of every leg** with `BUILD_MODE=push` (`:77-79`), plus a **re-scan** (`docker-build-push.sh` scans unless `SKIP_SECURITY_SCAN=true`); `--yes` | H go |
| smoke (`85-smoke.sh`) | **DOWN**, but checks only the `opentranscribe` label (`:215`); never auto-stops | — | Hub images; `remote-arm64` context (warn, `:184-202`) | GPU 1 (via test-fresh-install) | stock | **`TO_BRANCH` defaults to `master`** (`test-fresh-install.sh:49`), so it installs master's deployment files | H: stop the stack |
| promote (`90-promote.sh`) | — | — | Docker Hub | — | — | newest-published resolution | H go |
| finish (`95-finish.sh`) | — | — | `gh` auth | — | — | `release-validate.yml` green on the tag SHA (`:109-122`, no waiting); SBOMs per leg in the scan dir (`:205-260`) | H go |

### 2b. `run-dev-tests.sh` / `run-integration-tests.sh` phases (as driven by `60-test.sh:66`)

| Phase | Needs | Auto today | Gap / manual |
|---|---|---|---|
| preconditions (`run-dev-tests.sh:313-353`) | backend at :5174, a cpu queue that actually consumes, overlays | A (cpu-queue liveness probe) | `COMPOSE_PROJECT_NAME` must be exported for otfresh |
| `assert_fresh_stack_isolated` (`compose-project.sh:148-190`) | otfresh containers named `<project>-*`, Postgres on a named volume | A | — |
| 0. Tier-A sweep | API + DB on the dotenv ports | A | — |
| 1. Unit/API (ceiling 151, `:265`) | Postgres/MinIO/OpenSearch (conftest TCP probe) | A | 82 actual skips: HF token not exported to the venv (14), GLiNER weights (2), Redis (4), venv-eval + :5195 (6), search-quality (38), others. **Ceiling stale** |
| 2. FIPS (ceiling 2) | same | A | — |
| 3. Integration (ceiling 7, `:219`) | Redis env (A), mock-asr/llm (A), diar-native (A, conditional), HF token in the stack (export capability) | partly | **corpus (H)**, **`OPENSEARCH_TEST_MODEL_ID` (never)**; multi_gpu deselected unless `--gpu-scale` |
| 3b. dependency parity | running backend container | A | — |
| 4. GPU (ceiling 12) + diar-native smoke | host CUDA; sidecar | A | the 12 container-only skips are permanent; their real runner (`run-diarization-gpu-tests.sh`) is **never part of the release** |
| 5. schema drift, 5b session lifetime, 6. collection determinism | DB / static | A | — |
| 7. mutation ratchet (`:749`) | `.mutation/*` evidence that matches current hashes | A to read | **H to produce**: worktree + `--fresh` stack per stale module, then copy the evidence back |
| 9. e2e smoke | :5173/:5174, admin/password, registration open, Playwright | A | the e2e preflight refuses closed registration (`a9d99b1a7`) |
| 10. cleanup dry run | DB | A | — |
| (not in release) `--search-quality`, full e2e, `frontend-check`, `--with-gpu-diarization`, `--with-pipeline-smoke` | various | — | never run by `release.sh`; CI covers frontend only on a PR, and the release branch has none |

---

## 3. Q2: making `release.sh test` self-sufficient (corpus, model id, ceiling)

### 3.1 What the 7 was, and what it is now

- **2026-09-07 derivation** (`run-integration-tests.sh:183-219`; `backend/tests/CLAUDE.md`
  "Why the ceiling is 7"):
  - 4 × `test_fusion_strategy_switch.py` (`:209,222,234,252`: 7 matching chunks, 10 needed)
  - 3 × `test_rag_eval_harness.py` (`:61,73,100`: manifest injected into another cluster)

  Every one of them is corpus-dependent.
- **2026-09-29:** `test_search_completeness_opensearch.py::test_hybrid_reaches_every_keyword_matching_file`
  (`:502`, 4 params) is `@_needs_model`. `rg OPENSEARCH_TEST_MODEL_ID` finds it only inside that
  test file (`:21,26,43,55`), so these 4 **have never run in any gate**.
- **Today, fresh stack** (`release-test3.log:2130-2139`): 6 fusion (the 4 above plus `:323,:340`,
  which say "no user can access a chunk") + 3 rag-eval + 4 hybrid = **13**.
- **Today, corpus-rich dev stack:** if fusion has at least 10 matching chunks, it is 3 + 4 = 7. That
  passes at exactly the ceiling, with a **different seven** from the documented ones.

The ceiling has been silently absorbing a permanently-skipping test class. It should not be
re-tuned. The skip conditions should be removed, and a missing precondition should be reported by
name.

### 3.2 Recommended design (in order of preference), and why the guard is NOT the fix

**Option rejected as the primary fix: having the gate auto-inject QMSum into the otfresh stack.**
- It depends on NAS data (`inject-eval-corpus.sh:46`, `__main__.py:22`, 144 MB).
- It needs AMI timing data for real timings.
- It makes the gate's verdict depend on deployment state, which is the class the repo's hard rule
  bans.
- It forces a weaker live-stack guard on the critical path.
- The injected files would stay in the stack and change what the e2e/visual suites see. Visual
  baselines refuse a library that is not exactly the seed corpus
  (`scripts/CLAUDE.md`, seed-fresh-deployment).

**Primary fix: tests own their data (W1, W2), and the gate resolves the model id (W3).**

#### W1 — `test_fusion_strategy_switch.py` self-seeds a throwaway index (P0)

File: `backend/tests/integration/test_fusion_strategy_switch.py`. Copy the pattern from
`test_search_completeness_opensearch.py:192-262`.

1. Add a module-scoped fixture `seeded_index`:
   - name `test_fusion_<uuid4().hex[:12]>`. **Check it matches a registered index shape** in
     `scripts/lib/test_artifact_sweep.py`; if it does not, add one, or
     `unit/test_test_artifact_sweep.py` fails.
   - `client.indices.create(index=name, body=indexing_service._get_index_body_with_dimension(384))`,
     and per the teardown-discipline rule, the statement immediately after the create must be a
     `try` whose `finally` runs `client.indices.delete(index=name, ignore_unavailable=True)`.
   - bulk-load (`refresh=True`) about 60 deterministic chunk documents for one synthetic
     `USER_ID` (for example 4363):
     - at least 20 containing `budget`
     - at least 20 containing `marketing launch schedule`
     - about 10 containing both

     Each document needs `doc_type: "chunk"`, `file_uuid`, `chunk_index`,
     `accessible_user_ids: [USER_ID]`, `user_id`, `content`, `embedding_text`, `speaker`,
     `title`, `language`. Copy the base fields from `_file_docs` in the completeness test.
   - inside the module fixture, use a `pytest.MonkeyPatch.context()` to set
     `settings.OPENSEARCH_CHUNKS_INDEX = name` and
     `settings.OPENSEARCH_NEURAL_SEARCH_ENABLED = False`, and set
     `HybridSearchService._get_neural_model_id` to return `None`.
     `chunk_retrieval.retrieve_chunks` reads `settings.OPENSEARCH_CHUNKS_INDEX` at call time
     (`chunk_retrieval.py:443,594`), so the chat-path class follows the patch.
2. Point `client` (`:106`) at `seeded_index`.
3. Convert `corpus_supports_ranking` (`:164`) and `chat_corpus_supports_ranking` (`:294`) from
   `pytest.skip` to **`assert`**. With a self-seeded corpus, a shortfall means seeding broke. It
   no longer means "the deployment is thin".
4. `corpus_user_id` (`:267`): keep the discovery, and also assert that it equals the seeded
   `USER_ID`.
5. `test_nothing_in_this_module_wrote_to_the_index` (`:351`) now measures the throwaway index.
   That is a stronger claim, and it also removes the interference this test had with the eight
   other modules that write `transcript_chunks` (`run-integration-tests.sh:466-476`). That
   interference is the documented blocker to parallelising the integration phase.
6. Update the module docstring: it no longer reads the deployment's corpus.

**Red-first (mandatory):**
- (a) In an `ot-redcheck.` `git archive HEAD` tree, run the **old** module against an otfresh
  stack whose `transcript_chunks` is thin. Expect 6 skipped.
- (b) Run the new module. Expect 0 skipped and all passing.
- (c) Mutation control in the redcheck tree: make `_run` drop `params={"search_pipeline": …}`.
  `test_the_normalization_arm_breaks_that_bound` **must fail**.
- (d) Capture before/after collected node ids and diff them; there should be zero differences
  (gate-hardening "mandatory proof").

**Risks:**
- `_build_filters` may need DB rows for the user. The completeness test runs with a rowless
  `USER_ID=1078` (`test_search_completeness_opensearch.py:58`), so this should be fine; verify it.
- `ensure_search_pipeline_exists` writes cluster-level search-pipeline metadata with production
  ids. That is unchanged from today.

#### W2 — `test_rag_eval_harness.py` self-seeds a QMSum-format fixture (P0)

Files:
- new `backend/tests/fixtures/qmsum_corpus.py`
- `backend/tests/integration/test_rag_eval_harness.py`

1. The fixture writer, `write_qmsum_fixture(root: Path) -> Path`, is a pure data module in the
   style of `fixtures/synthetic_corpus.py`.
   - It writes `<root>/QMSum-feedface00/data/Committee/all/{qmfix_0,qmfix_1}.json`. The directory
     name must match `_QMSUM_DIR_RE = ^QMSum-([0-9a-f]{7,40})$` (`adapters/qmsum.py:47`), and the
     adapter globs `data/<Domain>/all/*.json` (`adapters/qmsum.py:~93-97`).
   - Use **Committee**: that domain gets synthetic timings by design, so no AMI or ICSI data is
     needed.
   - Schema, verified against a real NAS file (`covid_0.json`): top-level keys `topic_list`,
     `general_query_list`, `specific_query_list` (items
     `{query, answer, relevant_text_span: [["start","end"]]}`, decimal strings, inclusive) and
     `meeting_transcripts` (items `{speaker, content}`).
   - Write about 40 turns per meeting with 3-4 speakers, and 6 specific queries per meeting whose
     spans cover multiple turns. Include some "according to <Speaker>" queries, because
     `corpora.load_qmsum_queries` carves `SPEAKER_ATTR` (`corpora.py:57`).
   - The text is original, so it carries no licence issue.
2. Replace the `injected` fixture (`:34-56`). Follow the pattern in
   `test_corpus_injection_synthetic_e2e.py:103-130`:
   - `adapter = QMSumAdapter(write_qmsum_fixture(tmp_path_factory.mktemp("qmsum")))`
   - Seed `SEED = f"pytest-{uuid.uuid4().hex[:12]}"`. It must fully match
     `INJECTED_TEST_SEED_SHAPE` (`scripts/cleanup-test-data.py:295`), so a killed run is swept.
   - Pick the owner the same way as `owner_id` in the synthetic e2e test (first user).
   - For each meeting, call `inject_meeting(...)`, commit, and call
     `dispatch_indexing(record, owner, mode="eager")`.
   - Write the manifest with `manifest_mod.write(tmp_manifest_dir, adapter.describe(), records,
     turns, seed=SEED, tool_version="pytest", target=describe_target(), dispatch_mode="eager")`
     (`manifest.py:65-77`). `corpora.load_manifest` and `load_turns` read that manifest, and
     `load_qmsum_queries` finds the meeting JSON through `corpus.root/data/<domain>/all`
     (`corpora.py:259-260`).
   - In the `finally`: refresh the chunks index, then call `purge_injected(session, owner, "qmsum",
     SEED)` and assert `report.clean`.
3. Delete the fixed `MANIFEST_DIR` (`:31`) as the default. Optionally keep a
   **`RAG_EVAL_MANIFEST_DIR`** env override, so an operator can still point the test at a real
   injected QMSum corpus. In that mode the skips stay, but that mode is not part of the gate.
4. Turn the remaining "not judgeable" skips (`:85,:121,:156`) into assertions. With the fixture's
   known gold spans, finding nothing means the join is broken.

**Red-first:**
- Old module on a fresh stack: 3 skipped.
- New module: passes.
- Negative control: in the redcheck tree, corrupt the fixture's `relevant_text_span` so it points
  past the last turn. `test_gold_spans_map_onto_indexed_chunks` must fail with "mapped to zero
  chunks".

**Risk:**
- This injects into the production `transcript_chunks` of whatever stack is targeted, just as the
  synthetic e2e test already does. It is torn down by `purge_injected`, and a crash is backstopped
  by the sweep. Run it serially, which it already is.
- `QMSumAdapter.load` must succeed without `ami_root`/`icsi_root` for the Committee domain. Read
  `adapters/qmsum.py:97-140` to confirm the synthetic-timing branch.

#### W3 — the gate resolves `OPENSEARCH_TEST_MODEL_ID` (P0)

File: `scripts/run-integration-tests.sh`, beside the `REDIS_ENV` block (`:550-564`).

1. Hoist the `_backend` lookup out of the `if [ -z "${REDIS_HOST:-}" ]` block, so it is computed
   once.
2. Wrap the block in `# --- BEGIN integration-env ---` / `# --- END integration-env ---` markers
   so a unit test can extract it (the pattern in `40-build.sh:142-196` and
   `test_build_bare_tag_host_arch.py`).
3. If `OPENSEARCH_TEST_MODEL_ID` is unset and `_backend` is non-empty:
   ```bash
   _mid_out="$(docker exec "$_backend" python -c 'from app.services.search.ml_model_service import get_ml_model_service as g; print("MODELID:" + (g().get_active_model_id() or ""))' 2>/dev/null)" || _mid_out=""
   ```
   Take the `MODELID:` line with parameter expansion or a `while read` loop. Do **not** use
   `| grep -q` or `| head` (the pipefail rule in `scripts/CLAUDE.md`).
4. Verify the model with `curl -s "http://localhost:${OPENSEARCH_PORT:-5180}/_plugins/_ml/models/$id"`:
   - `model_state == "DEPLOYED"`
   - `model_config.embedding_dimension == 384`. The completeness test hardcodes 384 in
     `_get_index_body_with_dimension(384)`, `test_search_completeness_opensearch.py:222`.
5. If both hold, add `OPENSEARCH_TEST_MODEL_ID=$id` to an `INTEGRATION_ENV` array. It goes to the
   integration phase only, like `REDIS_ENV`. Otherwise print one yellow line naming the reason
   (no model / not deployed / dimension N). The 4 tests then skip and, with ceiling 0, the phase
   reports NOT MEASURED. That is honest.
6. An operator-exported `OPENSEARCH_TEST_MODEL_ID` always wins.

**Red-first test:** new `backend/tests/unit/test_integration_gate_model_id_provisioning.py`. It
extracts the marked block and runs it with a fake `docker` and fake `curl` on PATH. Cases:
- deployed 384 → exported
- empty id → not exported, warning
- dimension 768 → not exported, warning naming 768
- docker exec fails → not exported
- pre-exported value → untouched

At HEAD the markers do not exist, so the test fails.

**Risk:** with a model id set, the completeness test's `corpus` fixture embeds about 1,205 documents
through an ingest pipeline (`:197-221`, `request_timeout=600`). Measure the added seconds on the
otfresh stack. Expect tens of seconds on CPU MiniLM. It also changes the corpus fixture for that
module's non-hybrid tests (they now carry vectors); they must still pass.

#### W4 — set `INTEGRATION_SKIP_CEILING` to 0 (P0, lands after W1-W3 are measured)

- `scripts/run-integration-tests.sh:183-219`: rewrite the derivation comment (keep a derivation
  beside the constant; `unit/test_gate_phase_skip_accounting.py:229` expects one) and set the
  default to `0`.
- Update `backend/tests/CLAUDE.md` ("Why the ceiling is 7") and the docstrings of
  `backend/tests/unit/test_release_test_stage_gets_overlays.py:9-24` and
  `test_gate_phase_skip_accounting.py:17`. Those are prose only, but they would be stale.
- **Measurement before merging:** run the integration phase on an otfresh stack. It must show
  `0 skipped`, and the new test names must appear in the output, which proves it ran your code.

#### W5 (P1) — ownership-based guard for the injector, for operators

It is no longer on the gate's path after W1-W3, but it is still worth doing. Today
`guard_live_stack` (`backend/app/scripts/corpus_injection/env.py:105-120`) refuses any default
port. That wrongly refuses `otfresh-*` on stock ports, and it trusts nothing but numbers.

Replace the decision with **evidence about who owns the port**, and never use a name claim:
1. New `resolve_port_owner(port) -> tuple[project, container] | None`. Run
   `docker ps --filter publish=<port> --format '{{.Label "com.docker.compose.project"}}\t{{.Names}}'`
   via `subprocess.run(..., check=False, timeout=10)`. A docker error is a refusal (fail closed).
2. Allow without `--allow-live-stack` only when **all** of the following hold:
   - `POSTGRES_PORT`, `OPENSEARCH_PORT` and `MINIO_PORT` each resolve to exactly one container
   - all three belong to the **same** project
   - the project matches `^otfresh-[a-z0-9][a-z0-9-]*$`
   - every container in the project is named `<project>-*`
   - the postgres, opensearch and minio containers mount their data on `type == "volume"`
     (`docker inspect --format '{{json .Mounts}}'`), with no bind mount and no source path
     containing `.opentranscribe-live-data`

   This mirrors `assert_fresh_stack_isolated` (`compose-project.sh:148-190`) and adds the two
   extra stores.
3. **Never read `COMPOSE_PROJECT_NAME`.** An exported name is a claim; the container label is the
   evidence.
4. Otherwise keep today's rule exactly (default ports refused).

**Tests** (unit, fake `docker` on PATH):
- live `transcribe-app` on stock ports → refuse
- `otfresh-master` with named volumes → allow
- `otfresh-x` with a postgres bind mount → refuse
- ports split across two projects → refuse
- docker missing → refuse
- `COMPOSE_PROJECT_NAME=otfresh-x` exported while the labels say `opentranscribe` → refuse

**Reuse:** put the same port-owner resolution into a bash helper,
`scripts/lib/stock-port-owners.sh` (W6). That gives the repo one definition of "who owns this
port".

---

## 4. Q3: ordering, wall clock, redundancy, late discovery

### 4.1 The current critical path, with no failures

```
preflight 1m → bump 1m → verify ~5m → test ~60m → build ~60-70m → scan ~20-40m
→ [push] → rehearse ~30-60m (stack down, GPU 1) → tag → publish ~60m REBUILD + rescan
→ smoke ~10-15m (stack down) → promote 2m → finish (waits on CI)   ≈ 4.5-5.5 h serial
```

Every failure restarts its stage at full cost. Four builds and five scans on 2026-10-29 come to
about 6 h of rework.

### 4.2 Redundancy

| Duplicate work | Where | Fix |
|---|---|---|
| **Full image rebuild** of every leg already built in `build` | `80-publish.sh:77-79` (`BUILD_CACHE_REGISTRY` defaults off, `40-build.sh:73-89`) | P1: push the locally built leg tags plus `imagetools create` (the bytes are then scanned = rehearsed = published), or at least opt into `BUILD_CACHE_REGISTRY=true` and measure |
| **Re-scan** of every published image | `docker-build-push.sh` `run_parallel_scans` runs because `80-publish.sh` does not set `SKIP_SECURITY_SCAN=true` | P1: either skip it (the scan stage already covered these bytes once publish pushes them unchanged) or make it the single post-publish scan over registry digests |
| smoke's Hub fresh install ≈ rehearse scenario A | `85-smoke.sh:276-288` | it is justified only because publish rebuilds. Once the bytes are identical, reduce it to a digest-equality check, the capability probe, and a quick `up --wait` |
| unit/API suite ≈ CI `backend-tests` | — | not redundant (the local run has live services); but **CI never runs on the release branch**, so free coverage is wasted. Push at T0 and `gh workflow run pre-commit.yml --ref <branch>` |

### 4.3 Causes of late discovery

1. **Preconditions are checked at stage start, not at T0.** Known-in-seconds facts:
   - branch on origin (rehearse, `:309-318`)
   - stock ports and their owners (rehearse and smoke)
   - GPU 1 occupancy
   - Docker Hub and `gh` credentials (publish, finish)
   - mutation evidence (`run-mutation-tests.sh --check-baseline`)
   - visual-baseline freshness (a git comparison)
   - corpus and model presence (before W1-W3)
   - the platform resolvability of `requirements-lite.txt` on aarch64. This was the
     `ba36b99b0` cause: `torchcodec==0.11.1+cpu` is not published for aarch64.
2. **No same-commit invariant.** The ledger records `sha=` (`release.sh:85`) and nothing compares
   it. The scan's default output dir makes a post-build commit mandatory.
3. **Misattributed outcomes.**
   - Rehearse and smoke turn "ports busy" into a scenario FAIL (exit 1), not a precondition (exit 3).
   - `60-test.sh:71-73` maps NOT MEASURED (5) to **4**, and `release.sh:260-281` records 4 as
     `aborted` "operator declined a prompt — nothing ran", where `--force-test` cannot apply. The
     ledger then states something false about why the stage stopped.
4. **Load-sensitive tests in a 48-worker suite.** The billiard probe took 90 s and the static
   discipline scanners took 50-205 s each (`release-test3.log:1784-1810`). A red unit phase after
   about 16 minutes is the cost.

### 4.4 Proposed shape: a T0 "readiness" check, then a DAG

**T0: `release.sh preflight` gains these criteria** (add them to `release-criteria.yaml` under
`preflight`, warn unless noted, and record them in `10-preflight.sh`; the bidirectional rule means
both sides change together). Target: under 5 minutes.

| New criterion | Check | Would have caught |
|---|---|---|
| `branch-on-origin` | `git ls-remote --exit-code --heads origin "$(git rev-parse --abbrev-ref HEAD)"` | rehearse exit 3 |
| `mutation-evidence-current` | `./scripts/run-mutation-tests.sh --check-baseline` exit 0 vs 4; print the stale modules | run 2's NOT MEASURED, about 1 h in |
| `visual-baselines-fresh` | move `30-verify.sh:197-224` into a sourced function, call it from both | verify-time recapture |
| `stock-ports-free-or-owned` | W6 helper: list owners of 5173-5183 with stop commands | rehearse/smoke misfire |
| `gpu-1-headroom` | `nvidia-smi --query-gpu=index,memory.used,memory.total --format=csv,noheader` (read-only) | rehearse OOM or contention |
| `registry-credentials` | Docker Hub: credentials entry present for `index.docker.io` (check the key exists in `~/.docker/config.json` `auths`/`credHelpers`, **never print values**); `gh auth status` | publish/finish at hour 4 |
| `test-stack-ready` (only when a stack answers :5174) | new `run-integration-tests.sh --check-env`: resolve project identity (`assert_fresh_stack_isolated`), Redis env, model id (W3 block), registration enabled, HF token set in the backend env, cpu-queue liveness | runs 1 and 3's integration NOT MEASURED |
| `lite-arm64-resolvable` (P1) | extend `scripts/check-requirements-resolve.py` with `--platform manylinux2014_aarch64 --only-binary=:all:` for `requirements-lite.txt` | `ba36b99b0` |

**Then the DAG** (P1 orchestration in `release.sh`; until then, run it by hand in three shells):

```
T0 preflight+verify (≤10m) ──┬─ push branch + gh workflow run pre-commit.yml (CI in parallel, external, --yes)
                             ├─ Lane A: test (~60m, otfresh stack; integration phase is serial/low-CPU)
                             ├─ Lane B: build arm64 legs on the remote builder (no local CPU) → scan each leg as it lands
                             │          then amd64 legs (overlap with Lane A's serial integration phase)
                             └─ Lane C: mutation lanes for stale modules only (worktree + --fresh each)
join → rehearse (stack stopped, GPU 1) → tag → publish (push built legs, ~10-15m) → smoke → promote → finish
```

Estimated: max(60, about 70) + 45 + 15 + 15 + 10 ≈ **2.5 h**, with failures found in the first
5-10 minutes. Two caveats:
- Running Lane B's amd64 legs alongside Lane A's unit phase is load contention (`scripts/CLAUDE.md`
  timing rules). Start the amd64 legs when the unit phase ends, or `nice` them. Measure twice.
- Running test and rehearse concurrently would need the gate to be port-offset-aware (it hardcodes
  stock ports) and GPU 2 for one of them (root `CLAUDE.md` allows GPU 2 when idle). That is P2.

---

## 5. Q4: prioritised changes

Effort assumes one agent with red-first proof. "Safe now" means it can land on
`release/prep-0.6.0` without changing image contents. Any commit still forces a rebuild because of
the baked `GIT_SHA`. That is acceptable, because the build is already stale (section 1).

### P0: needed before v0.6.0 can be cut automatically

| ID | Change | Files | Effort | Safe now? |
|---|---|---|---|---|
| P0-1 | W1 fusion test self-seeds a throwaway index | `backend/tests/integration/test_fusion_strategy_switch.py` (+ sweep shape if needed) | 3 h | yes (test only) |
| P0-2 | W2 rag-eval test self-seeds a QMSum-format fixture via `inject_meeting`/eager/`purge_injected` | new `backend/tests/fixtures/qmsum_corpus.py`, `backend/tests/integration/test_rag_eval_harness.py` | 4-5 h | yes (test only) |
| P0-3 | W3 gate resolves and verifies `OPENSEARCH_TEST_MODEL_ID` | `scripts/run-integration-tests.sh` (+ unit test) | 2 h | yes (one phase's env) |
| P0-4 | W4 integration ceiling 7 → 0, after measuring 0 skips | `scripts/run-integration-tests.sh`, `backend/tests/CLAUDE.md`, two docstrings | 0.5 h | yes, once P0-1..3 are measured |
| P0-5 | W6 stock-port owner precondition: rehearse/smoke exit **3** naming the owning project and the exact stop command (`./opentr.sh stop --fresh <n>` for `otfresh-*`); preflight warns | new `scripts/lib/stock-port-owners.sh`; `65-rehearse.sh` (after `:77`, before `:145`); `85-smoke.sh:215`; `10-preflight.sh:325-335` (+ yaml only if a new id is added) | 2-3 h | yes |
| P0-6 | Same-commit invariant: new blocking criterion `artifacts-match-tagged-commit` in `70-tag.sh` | `70-tag.sh`, `release-criteria.yaml` (`tag:`), unit test | 2 h | yes (tag-only, overridable `--force-tag "reason"`) |
| P0-7 | **Operational, no code:** run `scan` with `OT_SCAN_DIR=.release/v0.6.0/security-reports`, so the worktree stays clean after scan. `50-scan.sh:23-35` writes the breadcrumb and `release-assets.sh:191-199` reads it for finish. Commit `security-reports/` afterwards via the master PR | — | 0 | yes |

**P0-6 detail.** For `test build scan rehearse`, read `.release/<v>/steps/<s>`:
- `status` must be in {`done`, `overridden`}
- `git rev-parse "<sha>^{commit}"` must equal `git rev-parse HEAD`

Otherwise record `fail`, naming every stale stage. Keep the early-exit `fail_out` contract
(`70-tag.sh:33-40`), and place the check after `tag-absent` and before `git tag`.

Unit test: a scratch git repo plus a ledger directory. Cases: all match → pass; build at
HEAD~1 → fail naming `build`; missing ledger → fail. Run the extracted block (BEGIN/END markers).
`test_release_criteria_wiring.py` checks the literal `record <id>`, so keep the `record` call at the
site.

### P1: soon after v0.6.0

| ID | Change | Effort |
|---|---|---|
| P1-1 | W5 ownership-based injector guard (`env.py`) | 3-4 h |
| P1-2 | `50-scan.sh` defaults `OUT_DIR` to `.release/<v>/security-reports` (code version of P0-7); document committing the reports after release | 1 h |
| P1-3 | Publish pushes the **built** legs (`docker push <repo>:vX.Y.Z-<cap>-<arch>` + `buildx imagetools create`), with no rebuild and no re-scan. Caveats: `--load` drops provenance attestations, and `80-publish.sh:206-223` already falls back to a config-blob check for that; frontend/docs need leg tags (`40-build.sh` comment `:130-133`). Fallback: `BUILD_CACHE_REGISTRY=true` measured, plus `SKIP_SECURITY_SCAN=true` | 1-2 d |
| P1-4 | Preflight T0 criteria in section 4.4, including `run-integration-tests.sh --check-env` | 1 d |
| P1-5 | `60-test.sh`: stop mapping 5 → 4 ("aborted"). Record `integration-gate not-measured` and exit **1**, so the ledger says `failed` with a not-measured criterion and `--force-test "reason"` is possible. Update `backend/tests/CLAUDE.md`'s two-contract note and `test_test_matrix_execution.py` if it pins this | 2 h |
| P1-6 | Rehearse auto-stop and restore for an `otfresh-*` owner (`./opentr.sh stop --fresh <n>`, restored in the existing trap with `start dev --fresh <n>`); smoke gets the same `--yes` / `RELEASE_AUTO_STOP_STACK` behaviour | 3 h |
| P1-7 | Rehearse asserts the local `davidamacey/opentranscribe-backend:$VERSION` label `org.opencontainers.image.revision` == `git rev-parse HEAD` (set at `docker-build-push.sh:609`) before scenario A, exit 3 "run build" | 1 h |
| P1-8 | `85-smoke.sh` passes `TO_BRANCH="$VERSION"`, so the smoke installs the tag's deployment files, not master's (`test-fresh-install.sh:49`). For v0.6.0 the manifest files are identical between `origin/master` and HEAD (checked: empty diff), so this is latent | 15 min |
| P1-9 | Re-derive `UNIT_SKIP_CEILING` 151 → 82 (`run-integration-tests.sh:243-265`, composition at `release-test3.log:1810-1858`); provision `HUGGINGFACE_TOKEN` (from the stack's backend env) to the unit phase to retire 14 skips; give the 12 container-only GPU suites a named deselect marker and add `--with-gpu-diarization` to `60-test.sh` (GPU 1, several minutes) | 0.5-1 d |
| P1-10 | `compose_project_name` falls back to the owner of `${POSTGRES_PORT:-5176}` when that is an isolated `otfresh-*` (W6 helper), so an unexported `COMPOSE_PROJECT_NAME` no longer breaks overlay detection | 2 h |
| P1-11 | Mutation-lane automation: `scripts/release/mutation-lanes.sh` (worktree + `--fresh` stack with a password-protected Redis + `--module` + copy `.mutation/<m>.{log,meta,testhash}` back + `--check-baseline`), started from T0 only for stale modules | 1 d |
| P1-12 | Accepted-risk file for scan (`security-reports/accepted-risks.yaml`: CVE, component(s), reason, issue, **expiry**), read by `50-scan.sh` and recorded as `overridden` with that reason automatically; expired entries block. This replaces typing the libxml2 reason on every release (v0.5.1 and v0.6.0 ledgers) | 0.5 d |
| P1-13 | `release.sh run --parallel` implementing the section 4.4 DAG | 1-2 d |
| P2 | Gate port-offset awareness (`run-integration-tests.sh:93-113`, `run-dev-tests.sh:314,355`, `60-test.sh:27` read `*_PORT`), so test (offset, GPU 2) and rehearse (stock, GPU 1) run concurrently; split the integration phase into throwaway-container suites in parallel and live-stack suites serial (unblocked by W1's isolation of the fusion test) | 2-3 d |

### Doc corrections (do them alongside the code that makes them true)

- `releasing.md:130`, `.claude/skills/release/SKILL.md` ("Validated bytes reach Docker Hub"),
  `40-build.sh:4-6` and `80-publish.sh:4-5`: false until P1-3 lands, because publish rebuilds.
- `SKILL.md` says "The local orchestrator and CI read the same file". The YAML header says there
  is no second consumer.
- `scripts/CLAUDE.md`: the integration phase is "~9 min"; it measured 33 min.
- `scripts/CLAUDE.md` / `lib/versions.sh` say the v0.3.3 hop is retained. For TO=v0.6.0 it is not
  (only v0.5.1 and v0.4.1). **Owner decision:** is upgrading from 0.3.x to 0.6.0 still supported?
  If yes, run `FROM_VERSIONS="v0.5.1 v0.4.1 v0.3.3"`.
- `test-fresh-install.sh:123` help says `LOCAL_IMAGE_TAG default 0.4.0`; the default is the
  VERSION file.

---

## 6. Q5: what remains for v0.6.0, and predicted first-run failures

### 6.1 Remaining steps, in order

1. Land P0-1..P0-6 on `release/prep-0.6.0`. There must be a **single writer**. Run
   `scripts/safe-precommit.sh run --all-files` and `--hook-stage pre-push`, and
   `python3 scripts/audit-tests.py backend/tests` must report 0 open findings.
2. `./scripts/release.sh preflight 0.6.0`, then `./scripts/release.sh verify 0.6.0`. Verify last
   passed at `2af0c7878`; P0 touches tests and scripts only, but re-run it.
3. Stack: `./opentr.sh start dev --fresh master` (stock ports), then
   `export COMPOSE_PROJECT_NAME=otfresh-master`.
4. `./scripts/release.sh test 0.6.0`. Expect integration `0 skipped`. The run must show the new
   test names.
5. `./scripts/release.sh build 0.6.0`. **Mandatory:** the images were built at `cc81ca19b`, the
   baked `GIT_SHA` differs from HEAD, and the runbook requires built = tagged
   (`docs/design/v060_release_runbook.md:44`).
6. `OT_SCAN_DIR=.release/v0.6.0/security-reports ./scripts/release.sh scan 0.6.0 --force-scan
   "<libxml2 CVE-2026-6653 reason, as in the ledger>"`. The override is pre-approved
   (runbook `:88-89`).
7. `./opentr.sh stop --fresh master`. Confirm `ss -tlnH` shows nothing on 5173-5183 and that GPU 1
   is idle.
8. `git push -u origin release/prep-0.6.0`. Optionally run
   `gh workflow run pre-commit.yml --ref release/prep-0.6.0`.
9. `./scripts/release.sh rehearse 0.6.0 --yes`.
10. With the owner's go for each step:
    - `tag --yes`
    - `publish --yes`
    - make sure the stack is stopped: rehearse restarts dev **only if it stopped it**
    - `smoke`
    - `promote --yes`
    - `finish --yes`. It needs `release-validate.yml` green on the tag SHA; if CI is still
      running, wait and re-run.
11. Open a PR `release/prep-0.6.0` → `master` (merge commit). Commit `.release/v0.6.0/security-reports/*`
    into `security-reports/` there. Verify `git merge-base --is-ancestor v0.6.0 origin/master`.

### 6.2 Predicted failures on the first real run

| Stage | Prediction | Evidence | Mitigation |
|---|---|---|---|
| test | load-sensitive unit timeouts recur (the gate is its own load) | `release-test3.log:440-449`; top durations 50-205 s at `:1784-1810` | run with no other writers, builds or mutation lanes during the unit phase |
| test | `--export-capability` downloads gated weights (229 s) | `:2104` | HF token in the stack `.env` |
| test | mutation ratchet NOT MEASURED if any `MODULE_TESTS` file changes during the P0 work | `release-test2.log:3257` | P0 touches none of those six modules' test lists; confirm with `run-mutation-tests.sh --list` before committing |
| build | ~1 h | ledger | — |
| scan | libxml2 CRITICAL blocks without `--force-scan` | ledger `scan` detail | step 6 |
| rehearse | **all three scenarios "FAIL" if `otfresh-master` still holds 5173-5180** (not detected by `:74-77`; `gr_die` exits 1) | `65-rehearse.sh:74-77`, `guardrails.sh:333-351` | P0-5 or step 7 |
| rehearse | exit 3: branch not on origin | `:309-318`; `git rev-parse @{u}` fails today | step 8 |
| rehearse | silently rehearses the stale `cc81ca19b` image if build is not re-run (local image reused, no revision check) | `test-fresh-install.sh:249-263` | step 5; P1-7 |
| rehearse | upgrade covers only v0.5.1 and v0.4.1 | `lib/versions.sh:277-311` | owner decision (section 5, doc list) |
| tag | `clean-worktree` fails because scan rewrote tracked `security-reports/*.txt` (if P0-7 is not used); a commit then breaks built = tagged | `50-scan.sh:23`; `5f5d4f458` did exactly that | P0-7, P0-6 |
| publish | ~1 h rebuild plus re-scan; Docker Hub credentials never pre-checked; the remote builder's DHCP lease may have moved since preflight | `80-publish.sh:41-79` | re-run preflight's builder check just before; P1-3, P1-4 |
| smoke | refuses or fails if the dev stack was restarted by rehearse (the `transcribe-app` project is not in `85-smoke.sh:215`'s filter, so the guardrails' `opentranscribe-*` **name** check fails it as a scenario FAIL) | `85-smoke.sh:215`, `65-rehearse.sh:124-140` | stop the stack before smoke; P0-5 |
| smoke | installs master's deployment files | `test-fresh-install.sh:49` | harmless for v0.6.0 (identical files); P1-8 |
| finish | fails if CI on the tag is still running (no wait) | `95-finish.sh:111-120` | re-run after CI completes. CI shellcheck of the release scripts passes locally (`shellcheck --severity=warning …` returned 0 today) |
| finish | SBOMs: present now in `./security-reports/*-sbom.json` (gitignored); with P0-7 they will be in `.release/v0.6.0/security-reports/` via the breadcrumb. Publish's re-scan writes `./security-reports/`, not the breadcrumb dir | `release-assets.sh:191-199` | fine; just do not delete the `.release` dir before finish |

---

## 7. Execution plan for the implementation agent

**Constraints.** Be the sole writer in the checkout, or work in your own worktree. Never run
pre-commit or commit with other writers active. Do not start or stop the live (NAS) stack. Use
`--fresh` stacks only, and only with the owner's go if the release `test` stage is in progress.
No GPU work without checking `nvidia-smi`. Never silence a finding.

1. **P0-3** (W3) and its unit test. Red first: the test fails at HEAD because the markers are
   missing.
2. **P0-1** (W1). Do the red-first steps (a)-(d) in an `ot-redcheck.` tree against a `--fresh`
   stack (`./opentr.sh start dev --fresh audit --port-offset 300`, then export
   `POSTGRES_PORT=5476 OPENSEARCH_PORT=5480 MINIO_PORT=5478` and Redis via the gate's resolution).
3. **P0-2** (W2), same procedure.
4. Run just the integration phase against the fresh stack with the W3 env. It must show 0 skips.
   Then apply **P0-4**.
5. **P0-5**: `scripts/lib/stock-port-owners.sh`.
   - `stock_port_owners` prints `port<TAB>container<TAB>project<TAB>working_dir`. Parse
     `docker ps --format '{{.Names}}\t{{.Label "com.docker.compose.project"}}\t{{.Label "com.docker.compose.project.working_dir"}}\t{{.Ports}}'`
     in pure bash, with no `grep -q`/`head`.
   - `stock_port_stop_hint` maps:
     - `otfresh-*` → `./opentr.sh stop --fresh <n>`
     - `transcribe-app` → `./opentr.sh stop`
     - working_dir ≠ repo → `cd <dir> && ./opentranscribe.sh stop`
   - Wire it into:
     - `65-rehearse.sh`: after the live-stack block, exit 3 with `record live-stack-stopped fail`
     - `85-smoke.sh`: before `:247`
     - `10-preflight.sh`: warn detail on the existing `live-stack` id

   Unit tests use fake `docker` and BEGIN/END extraction. Red: at HEAD, rehearse records
   `live-stack-stopped pass` with an `otfresh-master-postgres` publishing `0.0.0.0:5176->5432`.
6. **P0-6**: the tag criterion, the yaml entry and its unit test.
7. Run `scripts/safe-precommit.sh run --all-files`, then `--hook-stage pre-push`, and
   `python3 scripts/audit-tests.py backend/tests`. Make one commit per item, as conventional
   commits:
   - `test(search): seed the fusion A/B corpus instead of reading the deployment's`
   - `test(eval): seed a QMSum-format corpus for the eval-harness integration test`
   - `fix(gate): resolve the stack's embedding model for the hybrid completeness tests`
   - `test(gate): integration skip ceiling 7 -> 0 now that nothing depends on deployment data`
   - `fix(release): refuse rehearse/smoke when any stack owns the stock ports`
   - `feat(release): refuse to tag unless test/build/scan/rehearse ran on HEAD`
8. Hand back to the operator for section 6.1, steps 2-11.

**Verification that the plan worked:**
- `release.sh test` exits 0 on a fresh `otfresh-master`, with `Integration-marked tests passed (0 skipped)`.
- `release.sh tag --dry-run` shows `artifacts-match-tagged-commit` pass only after the rebuild.
- `release.sh rehearse` with `otfresh-master` up exits **3**, naming `./opentr.sh stop --fresh master`.
