# backend — OpenTranscribe FastAPI service

Orientation for the API + worker side. Repo-wide rules live in the root `CLAUDE.md`; this file
is backend-specific. Package-level `CLAUDE.md` files add detail where you're working.

## Layout

`app/api` (routers/endpoints) · `app/auth` (auth methods, see its CLAUDE.md) ·
`app/core` (config, constants, celery) · `app/db` (session + migration runner) ·
`app/models` (SQLAlchemy) · `app/schemas` (Pydantic) · `app/services` (business logic) ·
`app/tasks` (Celery tasks — the processing pipeline) · `app/transcription` (ASR/diarization
engine) · `app/utils` (shared helpers) · `app/middleware`.

## Commands

Prefer running inside the container (`./opentr.sh shell backend`). The host venv at
`backend/venv/` exists for pre-commit, mypy, ruff, bandit, and pytest outside Docker.

### Setting up the venv (new developer, ~5 minutes)

```bash
cd backend
python3.13 -m venv venv || python3.12 -m venv venv   # 3.13 preferred — see the note below
source venv/bin/activate
pip install -r requirements.txt                   # the app, fully pinned
pip install --no-deps -r requirements-nodeps.txt  # see that file's header for why
pip install -r requirements-dev.txt               # pytest, ruff, mypy, bandit, pre-commit
```

That is the **same** `requirements.txt` `Dockerfile.prod` installs, so the venv and the image
resolve to the same versions. There is no separate lock file and no Dockerfile-only install
step: if a package is only ever installed inside the image, it belongs in one of these files.

**There is no separate dev image.** `docker-compose.override.yml` builds every backend and Celery
service from `Dockerfile.prod`, bind-mounts `./backend:/app` over it, and runs
`uvicorn --reload`. So dev and prod share one dependency set — pinning `requirements.txt` aligns
the dev containers, the prod containers and the venv in a single move. (Only the *frontend* has a
`Dockerfile.dev`, for Vite HMR.) `Dockerfile.lite` and `Dockerfile.blackwell` install the same
`requirements.txt` too.

⚠️ **Interpreter parity is NOT guaranteed, only package parity.** `Dockerfile.prod` is
`python:3.13-slim-trixie`; this host ships 3.11 and 3.12, so `backend/venv` currently runs
**3.12.3** against a **3.13.12** image. The pins install cleanly on both and every resolved
version matches, so the gate compares the same package versions — but a dependency with a
`python_version` marker, or a bug that only reproduces on one minor, can still slip between them.
Install 3.13 on the host when you want that closed; it is not required to work here.

Verify parity at any time — this is the check that would have caught the drift #492 describes:

```bash
./scripts/check-dependency-parity.sh      # also phase 3b of run-integration-tests.sh
```

It diffs `pip freeze` in `backend/venv` against the **running** backend container, ignoring
`pip`/`setuptools`/`wheel` (build tooling, different by construction). Venv-only packages are
fine — `requirements-dev.txt` is deliberately not in the image. Image-only packages are a real
gap: the gate cannot exercise them.

Measured 2026-08-18 after this pinning landed: **281 packages shared, 0 image-only, 2 differing
(`pip`, `setuptools`)** — from 120 apart, 18 at a major version. ⚠️ It compares against the
container as **currently running**, so after changing a pin you must
`./opentr.sh rebuild-backend` before the check means anything.

**Optional, faster:** [`uv`](https://docs.astral.sh/uv/) is a drop-in installer that resolves
and downloads in parallel — noticeably quicker on this tree, most of which is CUDA wheels.
Nothing requires it; plain `pip` produces the same environment.

```bash
pip install uv
uv pip install --index-strategy unsafe-best-match -r requirements.txt
```

⚠️ `--index-strategy unsafe-best-match` is needed because the cu128 extra index carries an old
`requests`, and uv's safer first-index-wins default refuses to look at PyPI for a newer one.
pip does cross-index matching by default, which is why it needs no flag.

### Every spec is PINNED, and that is load-bearing (issue #492)

`requirements.txt` was **62 floors against 5 exact pins**, and a floor is a version number but
not a pin: `nltk>=3.9.4` permits 3.9.4 *or* 3.10.3. Two installs done at different times
legitimately resolved differently, and did — the venv and the image drifted **120 packages
apart, 18 at a MAJOR version** (starlette 0.48 vs 1.6, openai 2.44 vs 3.2, pandas 2.2 vs 3.0).

That is how the NLTK `pathsec` breakage reached production unseen: the venv got 3.9.4 and the
image 3.10.3, and 3.10 refuses multiply-linked files, so `split_sentences_nltk` raised on every
call in the backend and both transcription workers **while the host suite passed**. The gate was
structurally incapable of seeing it.

⚠️ **A branch name is a revision and still not a pin.** `pyannote.audio` is our own fork and was
installed from `@gpu-optimizations` — which *looks* specific, and is worse than a `>=` floor for
exactly that reason: two builds a week apart ship different diarization code from an identical
requirements file. It is pinned to a commit SHA in `requirements.txt` **and** in
`Dockerfile.blackwell`, which installs the same fork separately. Update both together.

To change a dependency: edit the `==` version, rebuild, run the suite. To refresh the whole tree,
resolve it with `pip-compile` (pip-tools) in a container matching the image's Python and copy the
versions back — do it deliberately, in its own commit, so a dependency bump is visible in a PR
diff instead of invisible in a rebuild. ⚠️ **Versions move forward only**: never downgrade a pin
to reproduce a result, because that reintroduces the divergence this closes.

Four files, one per environment, all fully pinned:

| File | Installed by |
|---|---|
| `requirements.txt` | `Dockerfile.prod`, `Dockerfile.blackwell`, the dev venv |
| `requirements-nodeps.txt` | the same, as a second `--no-deps` step |
| `requirements-lite.txt` | `Dockerfile.lite` (CPU-only image) |
| `requirements-ci.txt` | GitHub Actions (CPU-only) |
| `requirements-dev.txt` | the venv only — pytest, linters, pre-commit |

⚠️ **A model or dataset the app cannot run without belongs in a requirements file, not in a
`RUN` line.** `en_core_web_sm` — the spaCy pipeline Presidio tokenizes with — was installed by
`RUN python -m spacy download` in `Dockerfile.prod` alone. That resolved a version at BUILD time
(unpinned), and `Dockerfile.blackwell` had no such step at all, so that image shipped PII
redaction with no pipeline to tokenize with. It is now pinned by release URL in
`requirements.txt`, because spaCy models are not published to PyPI.

`alembic upgrade head` is **production-only** — dev applies migrations automatically on
backend startup via `app/db/migrations.py`.

## Conventions / patterns

- Keep files under ~300 lines; Google-style docstrings. Split oversized modules rather than
  growing them — see `docs/` and the per-package CLAUDE.md files for the existing splits.
- **Fat backend, thin frontend**: business logic, aggregation, and domain formatting belong
  here. The API sends pre-formatted display fields (`formatted_duration`, `display_status`,
  `resolved_speaker_name`, …) so the SPA renders rather than recomputes.
- **No mocking in production code paths** — mocks belong in test fixtures only.
- Tests that need Redis/Postgres/OpenSearch run against the real service or document the
  dependency; don't silently skip.
- **Vendor-clean core.** There is deliberately no managed-edition code in this repo (no
  `app/services/cloud` directory — the commercial repo injects its own at build time). Core
  code must never name the edition's vendors: CI's seam-guard greps `backend/app` (and
  `frontend/src`) for `clerk|stripe` and fails the build on a match.

## Gotchas

- Settings that look like `.env` vars are often **DB-backed** (`SystemSettings`) with coded
  defaults in `core/constants.py`, editable in the admin UI with no restart. Check there before
  adding an env var — several subsystems (redaction, watch sources, engine/boundary config)
  deliberately have **no** required `.env` entries.
- The GPU worker, CPU worker, and `celery-redaction` worker load different models. Don't import
  a model-loading module into a task that runs on the wrong queue.
