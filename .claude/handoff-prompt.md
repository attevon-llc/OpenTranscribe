# Handoff prompt — OpenTranscribe, 2026-08-24

Paste the block below to the next agent. Everything it needs is either inline or
behind a named file.

---

## PROMPT

You are picking up OpenTranscribe (`/mnt/nvm/repos/transcribe-app`) mid-flight.
Read `.claude/handoff-env-audit.md` first — it records what NOT to redo, and four
"obvious" cleanups in it are already-verified false positives.

Repo state: `master` clean and pushed at `2d31f4ee`. Dev stack up, 16/16 healthy.
No worktrees, no open PRs, no fresh deployments.

### ⚠️ Read before touching anything

1. **`.env` is gitignored — no git safety net.** Back it up and verify the
   checksum before any edit. Prove changes with a resolved-value diff, never by
   assertion:
   ```bash
   dump() { bash -c 'set +u; set -a; . "$1" >/dev/null 2>&1; set +a; env|sort' _ "$1" | rg '^[A-Z_]+='; }
   diff <(dump .env.backup) <(dump .env)
   ```
2. **Three secret-bearing backups exist**: `.env.backup`, `.env.bak`, `.env.bak2`.
   All gitignored. Delete once the stack has run a day.
3. **Never run `pre-commit` or `git commit` while anything else writes to the
   checkout** — it stashes the entire worktree (issue #434).
4. **Use `./opentr.sh`, never bare `docker compose`** — the overlay chain decides
   which database and storage you attach to.

---

## 1. BLOCKING — v0.5.0 cannot be tagged as-is

The release ledger reads:

```
✓ preflight  ✓ verify  ✓ test  ✓ build  ⚠ scan(overridden)  ✓ rehearse
  bump · tag · publish · smoke · promote · finish   ← pending
```

**The built images are STALE.** `244d26ad` changed `backend/app/utils/nltk_offline.py`,
`segment_dedup.py`, `text_preprocessing.py` and `services/search/chunking_service.py`
*after* the `build` stage ran. Those files are baked into the image.

Required before tagging:

```bash
./scripts/release.sh build 0.5.0        # rebuild on current master
./scripts/release.sh scan 0.5.0         # expect the same 20 unfixable CRITICALs
./opentr.sh stop
./scripts/release.sh rehearse 0.5.0     # requires the stack DOWN
```

`scan` will fail again — 20 CRITICALs, **zero with an upstream fix**, verified by
comparing installed vs `apt-cache policy` candidate for all 8 affected packages.
Override needs `--force-scan "reason"` and **David's explicit approval**; the
triage is issue #415. `tag`/`publish`/`promote`/`finish` are outward-facing and
are David's call, not yours.

Note `bump` shows pending; `VERSION` already reads `v0.5.0`, so check whether it
is a no-op before running it.

---

## 2. Issues to TACKLE

### #567 — six code defects (bug, backend, security, asr, gpu)
Each is *a documented setting that does not do what it says*. Highest value:

- **`GPU_CLUSTERING_DEVICE` is non-functional.** `speaker_clustering_service.py:979`
  hardcodes `torch.device("cuda:0")` and never reads it. On a multi-GPU box,
  clustering always lands on GPU 0 — which on this machine is a **reserved card**.
  Either wire it up or delete the `Settings` field.
- **FedRAMP AC-2 is documented but not implemented.** No task, endpoint or job
  reads `ACCOUNT_INACTIVE_DAYS`/`ACCOUNT_EXPIRATION_ENABLED`. This is a compliance
  claim — decide: build the sweep, or formally drop the AC-2 claim.
- Three ASR "alias" vars whose precedence exists only in orphaned `Settings`
  fields; `factory.py` reads the other name in each pair.
- SAML missing from `ENV_TO_CONFIG_MAPPING`/`DATA_TYPE_MAPPING` — works only by
  accident (`saml_enabled`.upper() happens to match), breaks on the next refactor.
- Dead constant at `constants.py:383`.

**Not yet filed, verify then add to #567** — three orphaned `Settings` fields with
zero readers, none in the template: `JWT_REFRESH_TOKEN_EXPIRE_MINUTES`
(`config.py:240`), `OPENSEARCH_TOPIC_SUGGESTIONS_INDEX` (`:492`),
`OPENSEARCH_TOPIC_VECTORS_INDEX` (`:493`).

### #566 — move UI-worthy env vars into the admin UI
Suggested order:
1. **VAD + Whisper quality** (8 keys) — smallest; a per-user precedent already
   exists (`transcription_vad_*`), the gap is a system-wide admin default.
2. **YouTube ingestion policy** (6 keys) — **no UI or DB layer exists at all**
   (verified: no `youtube.*` SystemSettings key, no admin endpoint, no frontend
   component). `YOUTUBE_USER_RATE_LIMIT_PER_HOUR`/`PER_DAY` are per-user quotas
   already tracked per user in Redis but configured globally;
   `YOUTUBE_AUTO_RETRY_ENABLED` is a kill switch needing a **container restart**.
3. **Search chunking/indexing** (~20 keys) — needs a reindex-required warning in
   the UI, ideally with a trigger.

The signal that a migration is done: `test_env_ui_setting_drift.py` starts
failing on that env var, telling you to delete it from the template.

### #559 — replace Perl exiftool (16 of 20 CRITICAL CVEs)
Do **not** adopt `exiftool-rs` on the strength of its "byte-for-byte parity"
claim — parity proves correctness on benign inputs, not safety. `ffprobe` is
already in the image with a working extractor emitting the same key shape
(`metadata_extractor.py`); measure what metadata is lost before reaching for a
new dependency. Tier-1 audit procedure is in the issue.

### #552 / #516 — document ingestion re-land (v0.6.0)
`feat/doc-ingestion` holds the full vertical. **Merge master in weekly, never
rebase, never squash.** The six-item semantic renumber checklist is in #552.

---

## 3. Issues to CLOSE

- **#553** — already closed (port pre-flight, fixed and merged via PR #554).
- **#415** — leave OPEN. It is the accepted-risk ledger for the perl CVEs and is
  correct as-is; the v0.5.0 triage is recorded as a comment.
- Nothing else from this session's work should be closed yet — #566/#567 are the
  open follow-ups by design.

---

## 4. What is DONE (do not redo)

- **`.env` cleanup**: `.env.example` 1937 → **200 lines**, 297 → 140 keys,
  1196 → ~35 prose lines. `.env` regenerated in lockstep, **zero value drift**.
  New `.env.test.example` (dev IdP credentials). docs-site env reference
  694 → 982 lines with `Where to set | limits | default | description` + examples.
- **Two new gates**, both red-checked: `test_env_file_shell_safety.py` (real
  `bash` source) and `test_env_ui_setting_drift.py` (**100 offenders** on
  pre-cleanup HEAD).
- **nltk pathsec fix** (`244d26ad`) — an unreadable corpus now degrades instead of
  failing the whole transcription, plus the harness fix that stopped hardlinking
  `nltk_data`. That bug had been silently failing `rehearse` for weeks.
- **Document ingestion excised** from master to `feat/doc-ingestion`; both lane
  PRs (#548, #549) merged; alembic head `v393`.

---

## 5. Verification ladder (cheapest first)

```bash
cd backend && venv/bin/python -m pytest -o addopts="" -q \
  tests/unit/test_env_file_shell_safety.py \
  tests/unit/test_env_ui_setting_drift.py \
  tests/unit/test_env_example_coverage.py       # 18 tests, seconds

./opentr.sh start dev --dry-run                 # resolves all overlays
./opentr.sh stop && ./opentr.sh start dev       # 16/16 healthy
./scripts/e2e/run-e2e.sh -m upload              # 25 tests, real processing
./scripts/run-integration-tests.sh              # the pre-merge gate
```

Every claim needs measured evidence. A green run that you cannot attribute to
your own code is not a measurement — check the test names in the output match
what you just wrote.
