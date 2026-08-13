# Full end-to-end audit of `chore/test-suite-perf-and-quality-overhaul` — findings

Answers `.claude/HANDOFF-full-audit.md` (task #27). Audited at HEAD `8332f7ce`, 2026-08-12/13.

**Method.** Five parallel read-only agents covering disjoint areas, with every load-bearing claim
re-verified in the main session before it appears here. All suites were run by one session against
the live dev stack. Where an agent's framing was wrong, the correction is stated in place rather
than quietly dropped — several are below.

**Bottom line: the branch is not ready for a PR.** Not because the work is bad — the test
engineering on it is genuinely strong — but because three of its headline claims do not survive
independent measurement, and the E2E suite it never ran is failing.

---

## Verdict on each of the five scope parts

| # | Scope | Verdict |
|---|---|---|
| 1 | Blind spots | **Found.** Seven new shapes; the two most serious are below. |
| 2 | Independent re-verification | **Two headline claims are wrong.** Route coverage and the survivor justification. |
| 3 | Compliance | **Controls are present, largely unenforced and substantially unexercised.** |
| 4 | Everything still works | **NO.** Backend green, CI-mode green, **E2E fails 8 + visual regression**. |
| 5 | Missing from the suite | **Substantial.** 41% of modules unreferenced; 30 stale documented numbers. |

---

## Measured myself

| Measurement | Result | Against claim |
|---|---|---|
| Backend, live stack (junit XML) | 6,591 passed · 146 skipped · 0 failed · 0 errors · **138.9 s** | ✅ claim holds |
| Backend, CI mode (`SKIP_S3=True SKIP_OPENSEARCH=True`) | 6,564 passed · 0 failed · 0 errors · 173 skipped · 146.0 s | ✅ the known CI-mode breakage **is fixed** |
| **E2E** (`./scripts/e2e/run-e2e.sh`) | **8 failed** · 266 passed · 59 skipped · 466 s, **+ visual regression failing** | ❌ **never run by the previous agent; not green** |
| Frontend (agent-measured) | 669 passed · 76 files · 21.2 s | ❌ docs say 481 / ~11 s |
| Sum of test times vs wall | 1,196.9 s / 138.9 s ≈ 8.6× parallelism | — |

Artifacts are legitimately current: the only commit after the recorded run added a doc file.
Machine load during measurement was ~37 on 48 cores; timings carry that caveat, except the 57.6 s
test in §5, which is deterministic.

---

## 1. Blind spots — what was NOT on the list

The handoff named seven shapes. These are the ones nobody had looked for.

### 1.1 A security mitigation that exists in the API and is used by nobody
`backend/app/utils/url_validation.py:105`. `resolve_public_addresses` returns the resolved
addresses **specifically so callers can pin them**; the docstring at `:118-119` says so in as many
words — "narrow the DNS-rebinding window between validation and connection". **No production
caller pins them.** The only callers are `is_safe_url` (`:178`, which discards them) and one test.

Every SSRF check in the product is therefore validate-then-re-resolve. `auth/oidc/discovery.py:76`
validates the URL, then `:85` hands the *hostname* to httpx, which resolves again. Same at
`api/endpoints/llm_settings.py:167` and `services/llm_service.py:1241,1645`.

This is the inert-gate shape applied to a security control, and it is a shape the branch never
looked for: not "a gate that does not run" but "a mitigation whose call site was never written".

*Severity qualified:* the OIDC issuer is super_admin-configured, so exploitation needs a malicious
admin config or DNS hijack of a legitimate IdP host. Discovery then runs at **login time**,
TTL-cached.

### 1.2 A guard whose absence of evidence is reported as evidence of absence
The mutation ratchet — covered in §2.3. Generalised: **this repo has no convention for
distinguishing "checked and passed" from "could not check".** Every gate here is binary, so any
gate whose input is optional silently degrades to a pass. `--check-baseline` was the live instance;
`30-verify.sh:92` (docs-build degrading to `warn` when a gitignored directory is absent) is a
second; `check_baseline`'s unmeasurable-coverage branch is a third.

### 1.3 A committed table that documents *why* a gate exists, and is factually wrong
`scripts/mutation-baselines.tsv` is read by the ratchet, so it escaped the "checked-in table
nothing reads" trap the repo knows about. It fell into the adjacent one: **it is read for its
numbers and believed for its prose.** Two of its notes are measurably false (§2.3). A triager
starts from "the 10 are log/error strings" and is pointed away from ten real predicates.

### 1.4 A test-honesty tool that is itself a test that cannot fail
`scripts/audit-route-coverage.py` — one of the four tools whose stated job is "to stop a test that
cannot fail from looking like a passing one" — **can never exit non-zero** (only `--selftest`
returns 1, at `:222`; both real paths `return 0` at `:282` and `:300`) **and has no automated
caller**. Verified: the only references in the repo are the script, two markdown files, and test
docstrings.

### 1.5 A self-test that passes while covering none of the tool's live defects
`audit-route-coverage.py --selftest` passes all 9 cases. None covers HTTP-method matching,
wildcard-versus-literal, or table-only references — the three defects that make its answer wrong
(§2.1). The branch's own doctrine is that a detector needs a must-fire case; the doctrine was
applied to `audit-tests.py` and not to its sibling.

### 1.6 A fix that disables the rule it was fixing
`scripts/triage-mutants.py` commit `b9bd10e3` — "a string inside a PREDICATE is logic, not noise" —
was correct in intent and **made the whole `noise-string` rule unreachable**. `_strip_strings`
substitutes the placeholder `<S>`, which contains `<` and `>`, and `_COMPARISON` matches `<|>`. So
`predicate_string` is True whenever `strings_only` is True, and `return 'noise-string'` at `:96` is
dead. Measured: 547 mutable lines across the six target modules, **rule 1 fired 0 times**. The
10-case self-test passes because its case 1 gets its verdict from a different rule.

### 1.7 A revocation-shaped defect class: reason strings that nothing asserts
Several survivors mutate the *reason* recorded in an audit or revocation row while the action still
happens correctly (e.g. `dependencies.py:309`). The system behaves right and records wrong. The
repo has no test convention for asserting the *content* of an audit row, which is why §3 finds so
many of these.

---

## 2. Independent re-verification — what the previous agent got wrong

Its self-corrected record was, as the handoff warned, incomplete.

### 2.1 "0 API routes with no test reference" is WRONG. True value ≈ 51.
Commit `b13e6213` claims 28 → 0. Independent re-derivation using a different method (require the
route path to be the first positional argument of an actual `<client>.<method>(...)` call, with
matching method, f-strings resolved to a fixpoint, wildcards permitted only against route
*parameters*) gives **≈51 uncovered, with a hard floor of 30 verified individually**.

Three matcher defects, all pointing the dangerous way:

1. **Method is not part of the match key.** `_segments_match` is called with `path` only
   (`:263-266`). A `POST` test covers a `DELETE` route. Live: five SCIM routes including
   `PUT /scim/v2/Users/{user_id}` — the RFC 7644 *replace* verb — have zero tests and score covered.
2. **An unresolved f-string wildcard may stand in for a *literal* route segment** (`:133`).
   `/api/files/\x00/\x00` matches every 4-segment route under `/api/files/`. 61 routes have only
   wildcard-bearing evidence. One such test (`test_media_security.py:109`) **asserts those routes
   are 404/removed** — a test proving a route is gone is counted as coverage for routes that exist.
3. **Any string constant in a test file counts, including route-inventory tables.** 30 routes have
   their only evidence in `test_route_has_a_caller.py` / `test_route_privilege_tiers.py` xfail
   tables and appear nowhere else in `backend/tests`. Adding a route to the xfail table makes it
   permanently "covered".

**The 28 was closer to right than the 0.** The metric was not satisfied; it was dissolved.

### 2.2 The survivor counts are honest; their *justification* is not.
All six counts verify exactly (180/149/73/10/133/91) against the logs, by two independent greps,
and the test-list fingerprints all match a recompute. Provenance is weaker than it looks: the six
`.log` mtimes span **13 ms**, so `/tmp/ot-mutation` was a curated artifact directory, not a run
directory; the `.testhash` mtimes spread across 47 minutes and are the real run markers.

**One genuinely stale measurement:** `security.testhash` is 22:11; commit `d8df6b6c`
("verify_token's exp check — `essential: False` makes a token immortal") landed 22:44. The security
baseline measures a superseded state, and `--check-baseline` reports "✓ holding at 133" with no
indication of it.

### 2.3 "Log-string and log-branch mutants are unobservable" is false as stated.
All 636 mutant bodies were regenerated **exactly** (mutmut's own pure `mutate_file_contents` into
`/tmp`; `backend/app/` never touched; all 636 names resolved to a matching
`x_<func>__mutmut_orig` with zero ambiguous function matches — the error that produced the previous
agent's worst mistake). Counted from the AST, not sampled:

| module | survivors | log-related | **not log-related** | est. real gaps |
|---|---|---|---|---|
| spans | 10 | **0 (0%)** | 10 | 1 |
| security | 133 | 10 (7%) | 123 | ~70 |
| password_policy | 91 | 34 | 57 | ~45 |
| lockout | 149 | 66 | 83 | ~70 |
| session | 73 | 33 | 40 | ~32 |
| dependencies | 180 | 50 | 130 | ~58 |
| **total** | **636** | **193 (30%)** | **443 (70%)** | **~275 (43%)** |

Two baseline notes are provably false:
- **spans: "The 10 are log/error strings."** `spans.py` has **no logger and no exception at all**.
  All 10 are predicates and default parameter values. The note is fabricated.
- **lockout: "77 log strings, 12 log-branch"** (=89). Measured **66**. Overstated by ~23 — and the
  same claim is repeated in the ratchet file's header as the *reason the ratchet exists*.

`security` is the sharpest case: **133 survivors, 10 log-related.** The residue is algorithm names,
claim names, hashing-scheme lists and FIPS predicates — exactly the "string inside a predicate is
logic" class that hid the FIPS-rehash finding.

### 2.4 The coverage pre-flight does not prevent the failure it was written for.
It is **module-aggregate** with a 60% floor; the failure was **per-function**.
`_enforce_proxy_identity_consistency` is 84 of 863 lines = 9.7% of its module, so deleting all its
tests keeps `dependencies` at ~83% — far above the floor — and re-manufactures the same 41 false
survivors. `dependencies` is at 93% coverage and *still* reports 9 survivors in that function.

### 2.5 Corrections to my own agents
- The compliance agent reported "FIPS mode does not change JWT signing" as a **critical FIPS
  violation**. The mechanism is real and verified (`direct_auth.py:73-75` signs with a hardcoded
  `HS256`; the FIPS-aware `_get_jwt_algorithm()` is reachable only from a `create_access_token`
  nothing imports). But **HMAC-SHA-256 is FIPS 140-3 approved** — no deployment is using
  unapproved crypto. The defects are a false documentation claim
  (`security-hardening.md:485` promises HS512), a dead code path, and a test that cannot fail.
  Reporting it as a crypto breach to a procurement reviewer would be an error.
- The inert-gates agent (and I) called the ratchet's skipping of unmeasured modules a bug. It is
  **deliberate and documented** (`run-integration-tests.sh:193-194`). The defect is the *reporting*,
  not the skipping.
- An early lead of mine — inconsistent `SKIP_S3` defaults across test files — is **not** a live
  bug: `conftest.py` uses `setdefault` after a TCP probe, so those per-file fallbacks are dead.

---

## 3. Compliance

The dominant failure mode is **not missing controls — it is controls that are present and mocked
out of their own tests**.

**Critical**
- **GDPR erasure reports success while transcript text survives.** Every OpenSearch step in
  `file_cleanup_service.py:359-410` is wrapped in `contextlib.suppress(Exception)` — transcript
  doc, `transcript_chunks` (RAG), summaries. `purge_media_file` still returns `deleted: True`,
  `summary["errors"]` stays empty, and `gdpr_erasure_service.py:341` audits SUCCESS. A brief
  OpenSearch outage leaves verbatim speech indexed and searchable, returns HTTP 200 with
  `errors: []`, and destroys the DB row that would identify what to re-delete. The voiceprint path
  *does* record its failures, with a comment explaining that biometric data surviving must not be
  recorded as complete; that reasoning was never applied to the largest personal-data store.
- **The biometric-erasure guarantee has zero executing coverage.** `_erase_speaker_voiceprints` and
  `_cleanup_opensearch_for_file` are `patch`ed in **every** test that reaches them.
  `test_erase_user_removes_rows` asserts `voiceprints_deleted == 3` — the mock's own return value.
  Replacing the function body with `return 0` fails nothing.

**High**
- **`DELETE /api/admin/users/{uuid}` writes no audit record** (`admin.py:814-885`). `audit_logger`
  is imported at `:29` and used at 1440/1474/1518/1676/1719 — never here. Its twin at
  `users.py:590` does the identical deletion and *does* audit. The destructive path is the
  unaudited one. *(Verified by reading the handler body.)*
- **Auth-method configuration changes never reach the audit log** — zero `audit_logger` calls in
  `auth_config.py` or `auth_config_service.py`. Switching the IdP, rotating the OIDC secret,
  disabling MFA enforcement or lowering the lockout threshold is invisible to
  `GET /admin/audit-logs`, the export, and the SIEM stream.
- **Platform GDPR erasure records the wrong actor 100% of the time** — `admin.py:2000` is the only
  caller of `erase_user` and passes no actor, so every record reads
  `actor_email: "data-subject-webhook"`. The org-admin twin 90 lines below does it correctly.
- **The bootstrap super_admin's password never enters password history**, and there is **no minimum
  password age** anywhere, so the reuse window is cyclable in minutes by an ordinary user.
- **Enabling FIPS silently invalidates existing MFA backup codes** — the FIPS backup-code context
  omits bcrypt, and `verify_backup_code` swallows the failure at DEBUG. The detector written for
  this case has zero production callers.
- **No durable erasure-request ledger.** Legal-hold deferral is permanently forgotten
  (`takedown_service.py:200-210` releases the hold and never calls back); backup restore
  resurrects erased subjects with no re-erase hook; the 30-day SLA is measured by nothing. Worse,
  under a hold the **account** survives entire — the Art. 17(3)(e) exemption is applied at account
  granularity when it only justifies file granularity.

Full store-coverage and password-path tables are in task #5/#13/#14 descriptions.

---

## 4. Everything still works — **no**

- Backend: green, live stack and CI mode both.
- **E2E: 8 failures + visual regression failures.** `test_gallery_actions.py` (organize dropdown,
  SRT/WebVTT/TXT bulk export), `test_tag_management.py` (promote to shared vocabulary, search
  clear), `test_auth_buttons.py` (navigation after login), `test_mfa.py` (Playwright timeout
  waiting for `.security-settings`). This is the suite the previous agent explicitly never ran.
- The pre-merge gate was still running at the time of writing; result to be appended.

---

## 5. Missing or wrong in the suite

- **41% of backend modules (218/532) have zero test reference.** Worst: `utils/auth_decorators.py`
  (257 LOC, four authorization decorators, no tests, and its only importer is itself untested);
  `services/search/chunking_service.py` (620 LOC) which decides the chunk boundaries feeding the
  RAG index flagged as the #52 leak trap; `tasks/youtube_processing.py` (876 LOC, untrusted URL
  input); `metadata_extractor.py` (574 LOC, 84 branches).
- **30 documented numbers are stale**, including the two the handoff itself quotes as ground truth
  (`CLAUDE.md:266-267`: 5,329/113 s and 481/~11 s versus 6,591/139 s and 669/21.2 s).
- **Three registered e2e markers select zero tests** (`gallery`, `smoke`, `api_e2e`) while root
  `CLAUDE.md:296` documents `-m gallery`. `test_pytest_config_consistency.py:118` checks
  used→registered but never registered→used.
- **One test is 41% of the wall clock** — `test_env_example_coverage.py:193` calls
  `_keys_read_by_code()` inside a loop over 26 compose files, with no caching. 57.6 s; next slowest
  is 9.9 s.
- **21+ auth/PKI E2E tests and 31 search-quality tests run in no gate at all.**
- **`security-scan.yml` cannot fail on a CVE** — every Trivy step omits `exit-code`, Dockle is
  `exit-code: 0`, Grype is `fail-build: false`; the summary job prints ✅ for scans it never read.
- **"Zero barrier clusters" needs qualifying**: true sub-second, but 21 of the 35 tests over 5 s
  are migration-consistency tests from 8 modules clustered near 9 s (the `ddl_exclusive` advisory
  lock).

---

## 6. Fixed during this audit

**Mutation ratchet no longer passes vacuously** — control-verified both directions.

Before: `OT_MUTATION_OUT_DIR=$(mktemp -d) ./scripts/run-mutation-tests.sh --check-baseline` →
**zero output, exit 0**. After: names all six unmeasured modules, exits **4**; the gate renders
`⊘ NOT MEASURED — proves nothing, not counted as a pass` instead of `✓ passed`. Evidence moved from
`/tmp` (cleared on reboot) to a gitignored `.mutation/` in the repo. The deliberate skip is
preserved; only the silence is gone. Details and the remaining holes: tasks #25, #16, #20.

*Recorded because it is the same class of error:* my first version of the log-adoption block ran for
any `OUT_DIR`, refilling the empty control directory and making the empty-evidence case
unreachable — I wrote the exact "guard no test can exercise" defect I was auditing for. It was
caught only because the control experiment failed to reproduce.

---

## 7. What I could not verify

- **Whether any individual survivor is still a survivor.** `--verify` transiently edits live source
  (off-limits) and `backend/mutants/` was cleaned, so `--show`/`--verify` return `UNVERIFIABLE`.
  Every "no test asserts this" rests on mutmut's recorded verdict plus code reading.
- **"Not asserted" vs "not executed"**, except where the mutant is guaranteed-fatal (~23 cases,
  where survival *proves* the line never executes).
- **Whether the `md5` calls actually raise under a FIPS-enforcing OpenSSL** — no FIPS provider here.
- **Whether audit writes reach OpenSearch in a real deployment** — failures fall back to
  `/var/log/opentranscribe/audit-fallback.jsonl`; I did not confirm that path is a mounted volume.
- **Whether the `verify_token` `exp`/`essential` gap is now closed** — the baseline says it was
  fixed after the run; nothing in the logs can confirm it.
- **Why `spans` produces no `pytest --cov TOTAL` line**, leaving its coverage unmeasurable and its
  floor recorded as 0.
- **Celery task-argument retention in the Redis broker** (personal data in serialized task args).
- **The deployed `password_history_count`** — DB-backed, not readable from the repo.

---

## 8. Coordination

`feat/rag-corpus-scale-403` is live in a worktree and its stack (`otfresh-rag403-*`) was running
throughout. It independently fixed #433's ordering and has its own
`test_segment_ordering_is_total.py`; fold the narrower fix and test into this branch's version at
merge. PR #364 touches the same auth surface as several findings above — check its diff before
proposing any auth change. Never run `pre-commit run --all-files` while that worktree is live
(issue #434).
