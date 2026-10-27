---
sidebar_position: 2
---

# Contributing

We welcome contributions to OpenTranscribe!

## Getting Started

1. Fork the repository
2. Clone your fork
3. Create a feature branch
4. Make your changes
5. Test thoroughly
6. Submit a pull request

## Development Setup

```bash
git clone https://github.com/YOUR_USERNAME/OpenTranscribe.git
cd OpenTranscribe
./opentr.sh start dev
```

## Code Guidelines

- Follow existing code style and patterns
- Add tests for new features (see [Testing Guide](./testing.md))
- Update documentation
- Keep commits focused
- Keep files under 200-300 lines
- Use Google-style docstrings for Python code
- Ensure light/dark mode compliance for frontend changes

### Backend Module Conventions

When adding new backend code, follow these module patterns:

- **Enums**: Add to `app/core/enums.py` (centralized, avoids circular imports)
- **Exceptions**: Add to `app/core/exceptions.py` (inherit from `OpenTranscribeError`)
- **Redis access**: Use `from app.core.redis import get_redis` (shared singleton)
- **Notifications**: Use `app/services/notification_service.py` for WebSocket notifications
- **Service interfaces**: Define Protocol classes in `app/services/interfaces.py`
- **Progress tracking**: Use `app/services/progress_tracker.py` (EWMA-based ETA)
- **Transcript formatting**: Use `app/utils/transcript_builders.py`

### Frontend Component Conventions

- Shared UI components go in `src/components/ui/` (BaseModal, Spinner, ProgressBar, SkeletonLoader)
- Use the shared components instead of creating ad-hoc implementations
- Component IDs are centralized in `src/lib/utils/ids.ts`

### Database Changes

All schema changes must use Alembic migrations:

1. Create migration in `backend/alembic/versions/`
2. Use idempotent SQL (`IF NOT EXISTS`) for safety
3. Update SQLAlchemy models in `backend/app/models/`
4. Add a detection guard for the new revision in `_detect_schema_version()` in `backend/app/db/migrations.py`
5. Test the full chain from scratch on a throwaway stack (`./opentr.sh start dev --fresh <name>`) and
   a rebuild-and-restart over an existing database. Avoid `./opentr.sh reset dev` unless you intend to delete
   the dev data.

The database is bootstrapped entirely through Alembic migrations. `database/init_db.sql` is a legacy
reference only and is not used. Details: `backend/app/db/CLAUDE.md` and `backend/alembic/CLAUDE.md`.

## Pre-Commit Hook Pipeline

OpenTranscribe enforces code quality through a multi-tool pre-commit pipeline. Each check exists for a specific reason:

| Hook (id) | Language | Purpose |
|------|----------|---------|
| **ruff**, **ruff-format** | Python | Catches common bugs, enforces consistent formatting. Replaces flake8, isort, and black with a single fast tool. |
| **mypy** | Python | Static type checking prevents runtime type errors in API contracts and database operations. |
| **bandit** | Python | Security-focused static analysis detects hardcoded secrets, SQL injection patterns, and unsafe function usage. The commit stage checks the staged files; the push stage scans all of `backend/`. |
| **import-linter** | Python | Enforces the import contract that `app` must not import cloud code. |
| **audit-tests**, **audit-session-lifetime**, **audit-error-disclosure** (each with a `-selftest`) | Python | Whole-tree auditors for tests that cannot fail, DB sessions held across slow work, and raw failure text on the wire. |
| **frontend-check** | TypeScript/Svelte | Commit stage: eslint, i18n key parity, svelte-check (`--threshold warning`), vitest and the frontend test auditor. |
| **frontend-build** | Frontend | Push stage: the same checks plus the production `vite build`. Catches import errors and build-time failures that would not surface during development. |
| **docs-build** | Docs | Push stage: `docusaurus build` of `docs-site/` (broken links and anchors fail it). |
| **prettier** | Frontend | Consistent formatting for TypeScript, Svelte, CSS, and JSON files. |
| **shellcheck** | Bash | Validates shell scripts for common pitfalls (unquoted variables, missing error handling). |
| **hadolint** | Docker | Lints Dockerfiles for best practices, via its pinned Docker image (Docker must be available). |
| **gitleaks** | Any | Secret detection. |
| **version-consistency** | Release | `VERSION`, `pyproject.toml`, `package.json`, `package-lock.json`, the Alembic single head and blog slugs must agree. |
| **conventional-pre-commit** | Git | Commit-message stage: enforces conventional commits (`type(scope): summary`). |

There are also housekeeping hooks (trailing whitespace, YAML/JSON/TOML validity, large files, merge
conflict markers, shebangs) and a few repository-specific guards; `.pre-commit-config.yaml` is the full inventory.

The frontend hooks only trigger when `.svelte`, `.ts`, `.js`, `.css`, or `.html` files under `frontend/src/` are staged. This avoids unnecessary build times when only backend files change. The hooks are split into two tiers: the commit stage is the fast edit loop, and the push stage carries the whole-tree jobs (`frontend-build`, `docs-build`, whole-tree bandit). CI runs both tiers, so run both before pushing (see below). Run `pre-commit install --install-hooks` once per checkout so the push and commit-message hooks are installed. If the Claude CLI is available during a commit, the frontend hook automatically attempts to fix failures before falling back to error output.

### Keeping the mypy hook honest

The mypy hook runs in its own isolated virtual environment, which contains only the packages
listed under `additional_dependencies` in `.pre-commit-config.yaml`. A package that ships type
information (`py.typed`) but is missing from that list is treated as `Any`, so the hook can pass
code that `backend/venv/bin/mypy` rejects. This happened: the hook reported 0 findings while the
venv reported 55 over the same 2,269 files, and nothing noticed until the two were compared.

The hook is therefore tied to the backend requirements rather than maintained separately:

- Its `rev` equals the `mypy==` pin in `backend/requirements-dev.txt`.
- Every dependency is an exact `name==version` pin equal to the pin in `backend/requirements*.txt`.
  **To bump a package, change it in the requirements file and in the hook in the same commit.**
- Every third-party module imported under `backend/` that ships `py.typed` is a hook dependency,
  except a short, reasoned `EXEMPT` table of heavy machine-learning packages (`torch`,
  `transformers`, `sentence_transformers`, ...). Those are type-checked by `backend/venv` only.
- The `types-*` stub packages in the hook are exactly the ones in `requirements-dev.txt`. A stub
  must never be added for a package that ships its own types: under PEP 561 the stub wins, which is
  how `types-redis` once hid redis 8's real signatures.

`backend/tests/unit/test_precommit_mypy_env_parity.py` derives all of this from the repository and
fails with the specific package to fix. Running mypy locally the way the hook does:

```bash
git ls-files 'backend/*.py' 'backend/*.pyi' | xargs backend/venv/bin/mypy \
  --config-file pyproject.toml --ignore-missing-imports --check-untyped-defs
```

CI also runs `scripts/mypy-hook-negative-control.sh`, which feeds the real hook three deliberately
mistyped lines in a throwaway repository and fails unless all three are rejected. Run it yourself
after changing the hook's dependencies; add `--ref <commit>` to judge an older configuration.

Run the full pipeline manually:

```bash
source backend/venv/bin/activate
scripts/safe-precommit.sh run --all-files                      # commit-stage tier
scripts/safe-precommit.sh run --all-files --hook-stage pre-push # push-stage tier (CI runs both)
```

## Testing Requirements

All contributions should include appropriate tests:

- **Backend changes**: Add unit tests in `backend/tests/`
- **Frontend changes**: Add E2E tests in `backend/tests/e2e/`
- **API changes**: Update API endpoint tests

### E2E Testing with Playwright

End-to-end tests use **pytest-playwright** to drive a real Chromium browser against the running development environment. This approach was chosen over mock-based testing because OpenTranscribe's authentication system (hybrid multi-method auth, MFA, session management) has complex interactions between frontend and backend that unit tests alone cannot validate.

**Test structure:**

| File | Coverage |
|------|---------|
| `test_login.py` | Form validation, success/failure, security headers, session persistence, UI state |
| `test_registration.py` | All fields, username/email/password validation, duplicate detection |
| `test_auth_flow.py` | Login, use application features, logout, session expiration |

The directory holds many more suites (gallery, upload, search, chat, settings, visual regression, a11y, ...); `ls backend/tests/e2e` lists them, and `backend/tests/CLAUDE.md` documents the markers and `RUN_*` gates.

**Key fixtures** (`conftest.py`):
- `login_page` -- navigates to login page, ready for input
- `authenticated_page` -- already logged in as admin (skips login flow)
- `auth_helper` -- helper methods for login/logout/register operations
- `api_helper` -- direct backend API calls for test setup/teardown

Tests can run headless (CI) or with a visible browser on an XRDP display for debugging:

```bash
# Headless (fast, CI-friendly) — the wrapper script is the supported entry point
./scripts/e2e/run-e2e.sh        # quick subset: ./scripts/e2e/run-e2e-smoke.sh
pytest backend/tests/e2e/ -v

# Visible browser (watch tests execute)
DISPLAY=:11 pytest backend/tests/e2e/ -v --headed

# Screenshots on failure only
pytest backend/tests/e2e/ -v --screenshot only-on-failure
```

**Requirements:** Dev environment running (`./opentr.sh start dev`), frontend at `localhost:5173`, backend at `localhost:5174`.

## Reporting Issues

Use GitHub Issues to report:
- Bugs
- Feature requests
- Documentation improvements

## Next Steps

- [Architecture](./architecture.md)
- [Testing Guide](./testing.md)
- [GitHub Repository](https://github.com/attevon-llc/OpenTranscribe)
