# backend/app/api — FastAPI routers, endpoints, WebSocket

## Purpose

Every HTTP/WS surface. Endpoints do auth gating, tenant scoping, and response shaping;
business logic belongs in `app/services`, pipeline work in `app/tasks`.

## Key files

- `router.py` — the single mount point. Register new routers here via
  `include_router_with_consistency(router, prefix, tags, capability=...)`, which normalizes
  the prefix (trailing-slash parity for nginx) and optionally attaches a capability gate.
  `main.py` mounts `api_router` under `settings.API_PREFIX` (`/api`).
- `endpoints/auth/` — **also the dependency-injection module.** `get_current_user`,
  `get_current_active_user` (~31 call sites), `get_current_admin_user`,
  `get_current_active_superuser`, `get_optional_current_user` are defined in
  `endpoints/auth/dependencies.py` and re-exported from the package — there is no `deps.py`,
  and `from app.api.endpoints.auth import get_current_active_user` still works. The rest of
  the package is one module per flow (`login`, `registration`, `profile`, `keycloak`, `pki`,
  `methods`, `mfa` + `mfa_tokens`, `sessions`), each owning its own `APIRouter` that
  `__init__.py` includes in declaration order.
- `deps_context.py` — tenant-aware DI: `get_current_context` → `RequestContext(user, org_id,
  org_role)`, `require_org_admin` (the **server-side** authority for org-admin actions — the
  frontend capability check is cosmetic), and `scope_to_context(query, model, ctx)`.
- `websockets.py` — `/ws`, the in-process `ConnectionManager`, and the Redis subscriber on the
  `websocket_notifications` channel.
- `endpoints/metrics.py` — `/metrics`, mounted at **root** (no `/api`), unauthenticated by design.
- `endpoints/chat/` — conversations, messages, export, projects, plus the user and admin
  settings routers. **`projects.router` is included BEFORE `conversations.router`**: both live
  under `/chat` and FastAPI matches in registration order, so `/chat/projects` would otherwise
  be shadowed. The admin settings router requires `get_current_admin_user` on **both** GET and
  PUT — the UI tab is cosmetic, that dependency is the authority.
- `endpoints/files/` — the oversized files router split into a package (`upload`, `crud`,
  `filtering`, `streaming`, `subtitles`, `reprocess`, `url_processing`, `waveform`, …).
  `management.py` exports a second router mounted at the same `/files` prefix.

## Conventions / patterns

- **Path params are UUIDs, never DB integers** (`/{file_uuid}`, `/{speaker_uuid}`). Resolve via
  `app/utils/uuid_helpers.py` — `get_file_by_uuid_with_permission` also applies the takedown
  and tenant gates, `require_resource_owner` replaces copy-pasted 403 checks.
- Guard with `Depends(get_current_active_user)` / `get_current_admin_user`. `role` is the
  authorization truth; `is_superuser` is only its derived mirror.
- Endpoints raise `fastapi.HTTPException` **directly** — deliberately NOT `core/exceptions.py`,
  which is reserved for the service layer (see its module docstring).
- Responses carry **pre-formatted display fields** built by `services/formatting_service.py`
  (`formatted_duration`, `display_status`, `resolved_speaker_name`, …). The SPA renders them.
- Read surfaces mask transcript text at read time via `services/redaction/spans.py`; owner
  reveal is `?redact=false` and is audited (`files/crud.py`, `files/subtitles.py`).
- **Owner-scoped listings go through `PermissionService.get_accessible_file_ids_subquery`** —
  it already covers owned files plus collections shared directly and via groups, and applies the
  org tenant gate. Never write a second sharing rule beside it.

## How it connects

Pydantic models in `app/schemas`, logic in `app/services`, dispatch into `app/tasks`.
See `backend/CLAUDE.md`, `backend/app/auth/CLAUDE.md`, `backend/app/services/CLAUDE.md`,
`backend/app/tasks/CLAUDE.md`.

## Gotchas

- `require_capability(key)` (`core/capabilities.py`) returns **404, not 403** — a gated router
  must look like an unknown route. Platform superusers bypass it.
- **`endpoints/tags.py` visibility rule** (`v374_add_tag_user_id`): a tag is visible if it is a
  system tag (`Tag.user_id IS NULL`), owned by the caller, **or** attached to a file in the
  accessible-files subquery — that's `_visible_to()`; `_owned_or_system()` is the narrower
  write/`/unused` scope. `_get_or_create_tag` takes a `user_id` and reuses the caller's row
  first, then a same-named system row. Tag names are only unique **per owner**, so
  `remove_tag_from_file` resolves the tag by joining `FileTag` for that file, never by name.
- `GET /api/auth/session` must **never 401** (200 for anonymous); it is the SPA's session probe.
- **Both SSE streams go check → subscribe → re-check.** `download_stream` (`files/__init__.py`)
  and `bulk_export_stream` (`files/subtitles.py`) read their readiness signal, subscribe to the
  pub/sub channel, then read it **again**. A single check-then-subscribe loses a worker
  completion published in the gap and the stream waits on `get_message` forever (#284 A1.22,
  #334). `test_handler_blocking_io.py` AST-pins the ordering for both.
- The WS endpoint `accept()`s *before* authenticating (cookie, else a 10 s first-message
  `authenticate` frame), then closes with 4001/4002/4003 — not HTTP status codes.
- Never touch `websockets.manager` from sync code; publish with
  `app/utils/websocket_notify.py:send_ws_event` (Redis pub/sub) so all API/worker processes reach
  the connection-owning process.
- Middleware order is load-bearing: `ObservabilityMiddleware` is added **last** in `main.py` so
  it runs outermost.
