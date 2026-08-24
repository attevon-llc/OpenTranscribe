# Handoff — `.env` / `.env.example` audit

**Status:** first pass complete and pushed (5 commits, `9ed68984` → `dbfc3b7b`).
**For:** an agent doing a deeper independent audit.
**Written:** 2026-08-24. **Amended 2026-08-24** after a full codebase audit reconciled against it —
two conclusions below are marked AMENDED with measured evidence.

> **This file is the "do not redo" record. The work list is
> `/home/superdave/.claude/plans/checkout-the-master-branch-async-mccarthy.md`**, which folds these
> findings in as tasks C1–C12 and cross-references #566/#567. Read the plan's
> **"⚠️ STATUS UPDATE — 2026-08-24"** section for the reconciliation.

Read this before re-deriving anything. Several conclusions here cost real
verification effort, and at least four "obvious" cleanups turned out to be wrong.

---

## The rule this file now follows

> **`.env` holds secrets and system config that must be readable before the
> database is. Anything configurable in the admin UI does not belong in it.
> Explanatory prose belongs in docs-site.**

Resolution order everywhere: **database > `.env` > coded default.** A UI value
always wins, so a setting documented in both places is a duplicate that will
drift — which is exactly how this file reached 1937 lines.

## What changed

| | before | after |
|---|---:|---:|
| `.env.example` lines | 1937 | **200** |
| prose lines | 1196 | ~35 |
| active keys | 297 | 140 |

New files:
- **`.env.test.example`** (62 lines) — throwaway dev credentials for the
  `--with-*-test` IdPs, monitoring and mock LLM. Kept out of the main template
  so published passwords never sit in something copied to production.
- **`docs-site/docs/configuration/environment-variables.md`** (982 lines) — now
  carries every moved variable with `Where to set | Valid values / limits |
  Default | Description` plus runnable examples.

Two new gates:
- **`backend/tests/unit/test_env_file_shell_safety.py`** — `.env` is
  shell-sourced by `opentr.sh`, so a value valid to compose can be broken to
  bash. Includes a real `bash` source, so it cannot pass on a theory.
- **`backend/tests/unit/test_env_ui_setting_drift.py`** — a key in
  `CATEGORY_SCHEMAS` may not ship as an active env var. Derives from the app's
  own registry, so a new UI field is covered the moment it is added.
  **Red-checked: 100 offenders on pre-cleanup HEAD.**

---

## ⚠️ Do not "fix" these — each is verified and deliberate

1. **`PKI_ENABLED`, `PKI_TRUSTED_PROXIES`, `PROXY_ENABLED`,
   `PROXY_TRUSTED_PROXIES` look redundant and must stay.** They are DB-backed,
   *and* read by a boot guard at `backend/app/main.py:94-133` that runs before
   any DB session. A hardened deployment refuses to start when either is enabled
   with an empty trust list — empty means "reject every header-sourced
   identity". Deleting them removes a fail-closed security gate.
2. **`AWS_ACCESS_KEY_ID` / `AWS_SECRET_ACCESS_KEY` / `AWS_REGION` are not
   ASR-specific.** The S3 storage backend uses them when `S3_USE_IAM_ROLE=false`
   and `BEDROCK_REGION` falls back to `AWS_REGION`.
3. **`CORS_ORIGINS` is documented in exactly one place** — inside the production
   hardening checklist. A bulk delete of that "obviously duplicate" block would
   remove the only documentation of a real security setting.
4. **FIPS / crypto profile stays env-only.** `FIPS_MODE`, `PBKDF2_ITERATIONS`,
   `JWT_ALGORITHM_V3`, `ENCRYPTION_ALGORITHM_V3` decide the crypto for data
   **already at rest**. A live DB toggle would change the lock while the data is
   encrypted with the old key.
5. **`ENCRYPTION_KEY` cannot be DB-backed** — it is the key that decrypts every
   DB-stored secret.
6. **Fusion knobs (`SEARCH_FUSION_STRATEGY`, `SEARCH_RRF_*`,
   `SEARCH_NORMALIZATION_*`, `SEARCH_COMBINATION_*`) are deliberately env-only
   measurement knobs.** `services/search/CLAUDE.md` argues this; a ten-arm sweep
   over 1,651 queries is why RRF is still the default.
7. **GPU device IDs are env-only** — workers read them at container start,
   before any DB, and they must match the compose device reservations.

## ⚠️ False positives already burned — do not re-report

- **`PROXY_*` flagged as ORPHANED by `env-audit`** — false. They resolve
  dynamically through `CATEGORY_SCHEMAS` / `env_var_for()`. Run the audit with
  `--exclude-prefix PROXY_` as well as OIDC_/SAML_/LDAP_/PKI_.
- **`UPLOAD_DIR` flagged as DERIVED-STALE** — ⚠️ **AMENDED 2026-08-24: half true. Both tests were
  right about different paths.** Re-measured against current master:

  | How `DATA_DIR` is set | `UPLOAD_DIR` resolves to | Follows? |
  |---|---|---|
  | a real **environment variable** | `/tmp/x/uploads` | **YES** ← the original test |
  | the **`.env` file** only | `/app/data/uploads` (the OLD default) | **NO** |

  Both class-body defaults call `os.getenv` at import, so an env var feeds **both** fields; a `.env`
  entry is applied by pydantic to **only the field it names**. The stated mechanism — *"Pydantic
  BaseSettings resolves the parent field before the child default is used"* — is **not** what
  happens. And the `.env` file is the documented operator mechanism, so this is the path that
  matters.

  **It is still not worth a DERIVED-STALE fix**, because nothing reads `UPLOAD_DIR` at all — the
  divergence is unreachable. Delete the field (plan task **C1**) and it disappears either way.
- **4 COMPOSE-BARE findings** — all in gitignored `reference_repos/open-webui/`,
  vendored code, not ours.
- **A "commented examples must be quoted" detector was written and deleted.** It
  cannot distinguish a real commented example from prose containing `KEY=value`,
  and this file is full of the latter (`# POSTGRES_HOST=postgres (already set
  above)`). An earlier draft corrupted five documentation lines before it was
  caught. The reasoning is recorded in
  `test_env_file_shell_safety.py`; if you re-add it, it needs an allowlist with
  written reasons, not a cleverer regex.

---

## Open work

### Tracked in GitHub

- **[#566](https://github.com/attevon-llc/OpenTranscribe/issues/566)** — env vars
  that *should* become UI settings. Candidate groups, in suggested order:
  1. **VAD + Whisper quality** (8 keys) — smallest slice; a per-user precedent
     already exists (`transcription_vad_*`), the gap is a system-wide admin default.
  2. **YouTube ingestion policy** (6 keys) — **no UI or DB layer exists at all**
     (verified: no `youtube.*` SystemSettings key, no admin endpoint, no frontend
     component). `YOUTUBE_USER_RATE_LIMIT_PER_HOUR`/`PER_DAY` are per-user quotas
     already tracked per user in Redis but configured globally by env;
     `YOUTUBE_AUTO_RETRY_ENABLED` is a kill switch that currently needs a
     celery-worker **restart** to change.
  3. **Search chunking/indexing** (~20 keys) — needs a reindex-required warning in
     the UI, ideally with a trigger.
- **[#567](https://github.com/attevon-llc/OpenTranscribe/issues/567)** — six code
  defects found by the audit, including a documented multi-GPU clustering feature
  that has never worked (`speaker_clustering_service.py:979` hardcodes `cuda:0`)
  and a FedRAMP AC-2 control documented but not implemented.

  ⚠️ **AMENDED 2026-08-24 — the setting IS non-functional (confirmed), but "clustering always lands
  on GPU 0, a reserved card" is REFUTED.** Verified on current master:

  ```
  speaker_clustering_service.py:979  torch.device("cuda:0")
    ← :486 _compute_similarity_groups ← batch_recluster ← speaker.recluster_all
  core/celery.py:225  "speaker.recluster_all":    {"queue": CeleryQueues.GPU}   ← device-ISOLATED
  core/celery.py:226  "speaker.cluster_for_file": {"queue": CeleryQueues.CPU}   ← never reaches cuda:0
  docker-compose.gpu.yml:37  celery-worker      device_ids: ['${GPU_DEVICE_ID:-0}']
  docker-compose.gpu.yml:52  celery-cpu-worker  count: all
  ```

  Clustering runs on the **GPU queue**, whose container is pinned to `GPU_DEVICE_ID` and renumbers it
  to index 0 — so `cuda:0` **is** the intended card. It does not touch a reserved GPU. The setting
  should still be fixed or deleted (plan task **C2**); it just is not a live GPU-safety incident.

  **The real latent hazard is the compose comment.** `docker-compose.gpu.yml:44` justifies
  `celery-cpu-worker`'s `count: all` with *"Speaker clustering uses GPU_CLUSTERING_DEVICE…"* — stale,
  since clustering is on the GPU queue. That worker **can** see every GPU including reserved ones, so
  if anything ever routes clustering to the CPU queue the hazard becomes real. Fix the comment
  alongside C2 (plan task **L3**). The other half of the `count: all` justification (nvidia-smi
  stats) is live and correct.

### Not yet filed — verify before acting

Three orphaned `Settings` fields found by `env-audit`, **none in the template**,
so they are dead code rather than misleading documentation:

- `JWT_REFRESH_TOKEN_EXPIRE_MINUTES` (`config.py:240`) — zero readers. Looks like
  a leftover from a units change; `JWT_REFRESH_TOKEN_EXPIRE_DAYS` is the live one.
- `OPENSEARCH_TOPIC_SUGGESTIONS_INDEX` (`config.py:492`) — zero readers.
- `OPENSEARCH_TOPIC_VECTORS_INDEX` (`config.py:493`) — zero readers.

### Remaining cleanup

- `.env.example` is 200 lines with ~35 prose lines. Further reduction is
  diminishing returns; the key set is settled.
- `.env` is 228 lines (140 template keys + 26 local settings kept in a labelled
  block at the bottom). It is regenerated from the template with values
  substituted, so the two stay structurally identical.

---

## How to work on this safely

**`.env` is gitignored — there is NO git safety net.** Back it up before every
change and verify the backup is byte-identical:

```bash
cp -p .env .env.backup && sha256sum .env .env.backup
```

Two backups currently exist and **contain secrets**: `.env.backup` (original,
pre-cleanup) and `.env.bak` / `.env.bak2` (intermediate). Delete them once the
stack has run for a day.

**Never assert a change is safe — prove it.** The pattern used throughout:

```bash
# resolved-value diff: must show ONLY the intended changes
dump() { bash -c 'set +u; set -a; . "$1" >/dev/null 2>&1; set +a; env|sort' _ "$1" | rg '^[A-Z_]+='; }
diff <(dump .env.backup) <(dump .env)
```

That is how the port dedupe was shown to be behaviour-neutral, and how the two
shell-sourcing bug fixes were shown to be the *only* semantic changes.

**The verification ladder, cheapest first:**

```bash
cd backend && venv/bin/python -m pytest -o addopts="" -q \
  tests/unit/test_env_file_shell_safety.py \
  tests/unit/test_env_ui_setting_drift.py \
  tests/unit/test_env_example_coverage.py     # 18 tests, seconds

./opentr.sh start dev --dry-run               # resolves all overlays, no unset vars
./opentr.sh stop && ./opentr.sh start dev     # 16/16 containers healthy
./scripts/e2e/run-e2e.sh -m upload            # 25 tests, real processing
```

**Run the auditor, but verify every finding:**

```bash
python3 ~/.claude/skills/env-audit/scripts/audit-env.py --selftest
python3 ~/.claude/skills/env-audit/scripts/audit-env.py . \
  --exclude-prefix OIDC_ --exclude-prefix SAML_ --exclude-prefix LDAP_ \
  --exclude-prefix PKI_ --exclude-prefix PROXY_
```

It reports **candidates, not verdicts** — 314 dynamic-access sites exist in this
codebase, so a field can be read without ever appearing literally.

---

## Suggested improvement to the `env-audit` skill

Two things would have saved time here:

1. **Exclude gitignored paths by default.** All four COMPOSE-BARE findings were
   in vendored `reference_repos/`. A `git check-ignore` filter removes that whole
   class.
2. **Auto-detect dynamically-resolved families.** The skill already warns about
   them and requires a manual `--exclude-prefix`. It could read a schema registry
   (here `CATEGORY_SCHEMAS` in `backend/app/schemas/auth_config.py`) and exclude
   those prefixes automatically — that is precisely what made `PROXY_*` a false
   positive while the four hand-excluded families were silenced.

A third, smaller: the DERIVED-STALE check flagged `UPLOAD_DIR`, which provably
works. Pydantic `BaseSettings` resolves the parent field before the child default
is used, so class-body derivation is not automatically stale — the check needs to
distinguish a plain class attribute from a `BaseSettings` field.
