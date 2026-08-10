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

- Shared UI components go in `src/components/ui/` (BaseModal, Spinner, ProgressBar, SkeletonLoader, ActionBox)
- Use the shared components instead of creating ad-hoc implementations
- Component IDs are centralized in `src/components/ui/ids.ts`

### Database Changes

All schema changes must use Alembic migrations:

1. Create migration in `backend/alembic/versions/`
2. Use idempotent SQL (`IF NOT EXISTS`) for safety
3. Update SQLAlchemy models in `backend/app/models/`
4. Update migration detection in `backend/app/db/migrations.py`
5. Test with `./opentr.sh reset dev`

There is no `init_db.sql` — the database is bootstrapped entirely through Alembic migrations.

## Pre-Commit Hook Pipeline

OpenTranscribe enforces code quality through a multi-tool pre-commit pipeline. Each check exists for a specific reason:

| Hook | Language | Purpose |
|------|----------|---------|
| **ruff** (lint + format) | Python | Catches common bugs, enforces consistent formatting. Replaces flake8, isort, and black with a single fast tool. |
| **mypy** | Python | Static type checking prevents runtime type errors in API contracts and database operations. |
| **bandit** | Python | Security-focused static analysis detects hardcoded secrets, SQL injection patterns, and unsafe function usage. |
| **svelte-check** | TypeScript/Svelte | Validates TypeScript types and Svelte template correctness. Runs with `--threshold warning` to catch warnings before they become errors. |
| **vite build** | Frontend | Verifies the production build succeeds. Catches import errors, missing modules, and build-time failures that would not surface during development. |
| **prettier** | Frontend | Consistent formatting for TypeScript, Svelte, CSS, and JSON files. |
| **shellcheck** | Bash | Validates shell scripts for common pitfalls (unquoted variables, missing error handling). |
| **Dockerfile lint** | Docker | Checks Dockerfiles for best practices (non-root user, layer ordering, security). |

The frontend checks (svelte-check and vite build) only trigger when `.svelte`, `.ts`, `.js`, `.css`, or `.html` files under `frontend/src/` are staged. This avoids unnecessary build times when only backend files change. If the Claude CLI is available during a commit, the hook automatically attempts to fix failures before falling back to error output.

Run the full pipeline manually:

```bash
source backend/venv/bin/activate
pre-commit run --all-files
```

## Testing Requirements

All contributions should include appropriate tests:

- **Backend changes**: Add unit tests in `backend/tests/`
- **Frontend changes**: Add E2E tests in `backend/tests/e2e/`
- **API changes**: Update API endpoint tests

### E2E Testing with Playwright

End-to-end tests use **pytest-playwright** to drive a real Chromium browser against the running development environment. This approach was chosen over mock-based testing because OpenTranscribe's authentication system (hybrid multi-method auth, MFA, session management) has complex interactions between frontend and backend that unit tests alone cannot validate.

**Test structure:**

| File | Tests | Coverage |
|------|-------|---------|
| `test_login.py` | ~50 | Form validation, success/failure, security headers, session persistence, UI state |
| `test_registration.py` | ~35 | All fields, username/email/password validation, duplicate detection |
| `test_auth_flow.py` | ~15 | Login, use application features, logout, session expiration |

**Key fixtures** (`conftest.py`):
- `login_page` -- navigates to login page, ready for input
- `authenticated_page` -- already logged in as admin (skips login flow)
- `auth_helper` -- helper methods for login/logout/register operations
- `api_helper` -- direct backend API calls for test setup/teardown

Tests can run headless (CI) or with a visible browser on an XRDP display for debugging:

```bash
# Headless (fast, CI-friendly)
pytest backend/tests/e2e/ -v

# Visible browser (watch tests execute)
DISPLAY=:13 pytest backend/tests/e2e/ -v --headed

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
