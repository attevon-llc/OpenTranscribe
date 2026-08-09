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
`backend/venv/` exists for pre-commit, mypy, ruff, bandit, and pytest outside Docker. It needs a
two-step install: `pip install -r requirements.txt` then `pip install --no-deps -r
requirements-nodeps.txt` — see that file's header. This is the same two-step sequence
`Dockerfile.prod` runs; there is no Dockerfile-only install step for this project. If a package
is only ever installed inside the image, it belongs in one of these two files instead.

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
