# Handoff — Document ingestion, end to end (#362)

**Owner:** a dedicated agent, in its own worktree.
**Branch:** `feat/doc-ingestion`, cut from `feat/rag-corpus-scale-403`.
**Merges back into:** `feat/rag-corpus-scale-403` — **not** `master`.
**Written:** 2026-08-14, from `feat/rag-corpus-scale-403` @ `693a16c1`.

---

## Why this is a separate lane

Documents are a different data shape with their own lifecycle. They share almost nothing with
the transcript pipeline except the sentence splitter. Meanwhile the RAG/chat lane is finishing
retrieval quality work, and a third lane is running multi-hour mutation experiments on
`chore/test-suite-perf-and-quality-overhaul`.

Three lanes, three worktrees, three working directories. That last part is not bookkeeping —
see **Git discipline** below.

## The honest starting position

**The parser is built. The feature is not.** `services/documents/` is a working library that
nothing calls.

| Layer | State |
|---|---|
| `backend/app/services/documents/` — `backends/`, `chunking.py`, `detect.py`, `ir.py`, `progress.py`, `protocol.py`, `registry.py`, `safety.py`, `types.py` | ✅ exists, tested |
| Tika sidecar container | ✅ stood up, cross-checked against docling on legacy OLE2 |
| **`Document` model / table / migration** | ❌ **does not exist** |
| **API endpoints** (upload, list, detail, delete) | ❌ **do not exist** |
| **Celery parse task** | ❌ **does not exist** |
| **Frontend** (upload, list, detail, progress) | ❌ **does not exist** |
| **Watch-source integration** (auto-import documents) | ❌ **does not exist** |

The clincher: `services/documents` is imported by exactly **two** modules —
`core/constants.py` and `search/chunking_service.py`. Nothing that ingests anything.
**A user cannot upload a document today.**

## Definition of done

A user can upload a PDF/DOCX/HTML/PPTX through the UI, watch it parse with real progress,
see it listed and openable, ask a chat question that retrieves from it with a working
citation, and delete it — with the document's text subject to the same redaction guarantees
as a transcript. Plus: a watch folder can auto-import documents.

Anything less is a partial vertical and should be said so plainly in the PR.

## Scope, in build order

Each step should be independently committable and green.

1. **DB** — `document` table + migration (see the numbering note). Decide deliberately whether a
   document is a `media_file` row with a discriminator or its own table. **Recommendation: its
   own table.** `media_file` is ~70 columns, loaded whole by every gallery page, and carries a
   large amount of A/V-specific state (duration, waveform, speakers, diarization) that is
   meaningless for a PDF. `file_facts` (v390) is the precedent for a narrow sidecar.
   Whatever you choose, write down why — the next reader will ask.
2. **Parse task** — Celery, on a CPU queue. Emits progress via the existing progress-tracker
   machinery (`services/documents/progress.py` already exists for this). Must be idempotent and
   re-runnable; parsing is expensive and a retry must not duplicate chunks.
3. **Indexing** — into the **existing v6 `transcript_chunks` index**, not a new one. It already
   has the `doc_type` discriminator (`chunk` / `digest`) and the plane-scoping helpers
   (`chunk_plane_clause`, `digest_plane_clause`, `file_plane_query`). Adding a third index would
   fork retrieval and break the router. Read `backend/app/services/search/CLAUDE.md` first.
4. **API** — upload (reuse the presigned/multipart path in `services/multipart_upload.py` —
   do not invent a second upload mechanism), list, detail, delete. Delete must sweep
   OpenSearch and object storage, not just the row.
5. **UI** — upload surface, list, detail, parse progress. Reuse existing components
   (`components/upload/`, the websocket progress store) rather than parallel ones.
6. **Watch sources** — document auto-import. `services/watch_sources/` has its own CLAUDE.md
   and a 3-layer imohash dedup you must not bypass.

## ⚠️ Migration numbering

Current high-water marks: `master` **v386** · `chore/test-suite…` **v389** ·
`feat/rag-corpus-scale-403` **v392**.

**Reserved for this lane: `v393`–`v399`.** The RAG/chat lane takes `v400+`; the chore lane
should take `v410+`.

This reservation is a convenience, not a guarantee — the chain will be re-linearised at the
final merge to `master` anyway, and the owner has accepted that. But observe it, because the
failure mode is nasty and we have already paid for it once:

> **Two branches adding the same revision number merge CLEANLY.** Different filenames means no
> textual conflict, so nothing in the conflict list warns you. The fork exists only in the
> `down_revision` graph and surfaces later as a failed `alembic upgrade head` — which, in dev,
> means the backend aborts on startup and reads as a broken stack rather than a bad merge.

The recovery, if it happens anyway: renumber, then re-stamp the affected database to the common
ancestor and let the idempotent chain re-apply. Four places move per revision — the filename,
`revision`/`down_revision`, the detection arm in `app/db/migrations.py`, and `REVISION` in its
consistency test. And note that **a rename sweep does not finish the job**: a
`down_revision == "v388…"` assertion stayed *valid* while ceasing to be *correct*, and only the
suite caught it. Full account in `backend/app/db/CLAUDE.md` ("Renumbering note 2").

## Local data

Corrupting the local dev database is **acceptable** — test data can be recreated.

**The one expensive thing is large sample-dataset ingestion.** Re-ingesting a corpus costs
hours. So:

- Use `./opentr.sh start dev --fresh docingest --port-offset 200` — its own compose project,
  its own volumes, and the NAS/bind overlay is **never** loaded, so the real dataset cannot be
  touched.
- If you need a corpus, use `--limit` first and prove the path on 10 files before ingesting
  thousands.
- Never run against the epic's `otfresh-rag403` stack (ports 52xx) — that holds the 210,908-doc
  index the RAG lane is measuring against, and re-indexing it is a multi-hour setback.
- `./opentr.sh data-paths` prints the resolved LIVE paths. A `.opentranscribe-live-data` marker
  in a directory means **do not delete**.

## Git discipline

```bash
# from the main checkout
git worktree add .claude/worktrees/doc-ingest -b feat/doc-ingestion feat/rag-corpus-scale-403
```

- **Separate worktrees have separate working directories AND separate indexes.** This matters:
  pre-commit **stashes the entire working tree** for the duration of a run. When two agents
  shared one worktree, a commit in one destroyed in-flight work in the other — repeatedly, in a
  single day. Across worktrees that is structurally impossible.
- Git refuses to check out the same branch in two worktrees. That guard is deliberate; don't
  work around it.
- `.env` is gitignored and does **not** come along. Copy one in.
- `backend/venv` cannot be built in a worktree (the dev stack's anonymous volume creates a
  root-owned stub). Borrow the main one:
  `OT_TEST_PYTHON=/mnt/nvm/repos/transcribe-app/backend/venv/bin/python ./scripts/run-backend-tests.sh`
- Commit and push `feat/doc-ingestion` only. Never merge onto `master`, never force-push.
- Shared files (`services/CLAUDE.md`, `api/CLAUDE.md`, `.env.example`, i18n locales) **will**
  conflict on merge-back. Keep edits additive and localised to reduce the surface.

## Six traps this codebase has already paid for

Read these before writing code; each one shipped, and each cost real time.

1. **The chunk index stores transcript text UNREDACTED.** Correct for search over your own
   content, but it means *any* path sending chunk content to an LLM must mask first. Document
   text lands in the same index, so it inherits this. `services/chat/CLAUDE.md` has the rules.
2. **Never hold a DB session across slow non-DB work.** A plain `SELECT` holds `ACCESS SHARE`
   for the life of its transaction, which queues every `ALTER TABLE` behind it — it hangs an
   Alembic upgrade mid-release, and dev runs migrations on backend startup. Parsing is *slow*;
   this is your highest-risk pattern. `scripts/audit-session-lifetime.py` enforces it and now
   also recognises `session_factory` / `_short_session` openers.
3. **A swallowed or absent signal is indistinguishable from a clean result.** This one shape
   produced six separate defects in this epic. `detect_segment_spans` swallowing a detector
   error, an absent Presidio reporting "no PII found", a `terms` agg returning a constant, a
   test asserting against an empty index. If your parser can fail partially, it must say so —
   see #69 below for the live example.
4. **⚠️ `session_scope` COMMITS on exit.** Mutating loaded ORM objects inside a Celery task
   therefore *persists* the mutation. This nearly destroyed transcripts in the export path
   (fixed in `693a16c1` by expunging before mutating). If your parse task mutates anything it
   loaded, check whether you meant to write it.
5. **Migration numbering** — above.
6. **`--summary` and other reporting must not lie.** A green signal that means nothing is worse
   than a red one. Watch every new test fail before you fix the thing it tests.

## Known live defect to fix in this lane

**#69 — docling-slim's HTML backend silently drops ~85% of a table-heavy document
(140 tables → 3).** Silent, so it looks like success. Fix this *before* any quality measurement
of document retrieval, or every number describes a parser that is discarding most of the
content.

## Testing bar

Production code, so: real tests that would fail if the code were wrong.

- Watch each new test fail first — in a `git archive HEAD` tree, so the working tree is never
  modified to produce a red run.
- Include controls. A fix that masks/parses/indexes *everything always* must not pass.
- `POSTGRES_PORT` must point at **your** fresh stack, not the default 5176 and not 5276.
- `python3 scripts/audit-tests.py backend/tests` and `python3 scripts/audit-session-lifetime.py
  backend/app` must contribute **zero** new findings.
- `cd backend && ruff check . && ruff format --check . && mypy app` — and **mypy `app` does not
  cover `tests/`**, so check test files explicitly. That gap blocked commits twice in one day.
- No `noqa`, no `type: ignore`, no allowlist entries. Fix the finding.
- Frontend: `npm run check` + `npm run build` + `npm run test` + `npm run test:audit`.

## Reporting back

Open a PR from `feat/doc-ingestion` into `feat/rag-corpus-scale-403` when the vertical is
complete, with: what works end to end, what does not, measured numbers (parse time per format,
index size, retrieval latency with documents in the mix), and anything found-but-not-fixed.
State partial completion plainly rather than implying a full vertical.
