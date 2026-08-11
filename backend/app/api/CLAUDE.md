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
  the package is one module per flow (`login`, `registration`, `profile`, `oidc`, `pki`,
  `methods`, `mfa` + `mfa_tokens`, `sessions`), each owning its own `APIRouter` that
  `__init__.py` includes in declaration order.
- `deps_context.py` — tenant-aware DI: `get_current_context` → `RequestContext(user, org_id,
  org_role)`, `require_org_admin` (the **server-side** authority for org-admin actions — the
  frontend capability check is cosmetic), and `scope_to_context(query, model, ctx)`.
- `websockets.py` — `/ws`, the in-process `ConnectionManager`, and the Redis subscriber on the
  `websocket_notifications` channel.
- `endpoints/metrics.py` — `/metrics`, mounted at **root** (no `/api`), unauthenticated by design.
- `endpoints/scim/` — SCIM 2.0, also mounted at **root** (`/scim/v2`, RFC 7644 §3.1 fixes the
  base path). Bearer-token authenticated, not session-authenticated, and deliberately **not**
  rate limited; its errors are SCIM Error resources via `main.py`'s `SCIMError` handler.
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
- **`endpoints/tags/` is a package** (`crud` · `discovery` · `sharing` · `operations` + `_common`),
  split like `endpoints/files/`. Every module registers onto the ONE router in `_common` —
  sub-routers were tried and rejected because FastAPI refuses an empty path when including a
  router with no prefix, and `POST ""`/`GET ""` are real routes. **Import order in
  `__init__` is load-bearing**: `/for-files`, `/impact` and `/promote` are literal paths that
  must register before the `/{tag_uuid}` routes. Do NOT re-export third-party names through
  `_common` — ruff prunes them as unused there and the backend stops booting.
- **The tag plane has THREE scopes, and mixing them up is the bug** (`v374_add_tag_user_id`).
  All three live in `services/tag_service.py` so nothing re-derives them:
  | Scope | Rule | Used by |
  |---|---|---|
  | `visible_to()` | system **or** owned **or** explicitly shared (`v386`) **or** attached to an accessible file | `GET /tags` (the list) |
  | `owned_or_system()` | system **or** owned | resolution, `/unused`, `/collisions` |
  | `_writable_tag_ids()` | owned always; system for an **admin**; else **404** | every mutation |
  Reading and rewriting are different rights: `visible_to` admits a tag on a file shared with
  you, but renaming it rewrites its owner's vocabulary everywhere. Narrowing the *list* to
  `owned_or_system` silently drops shared-file tags from the recipient's picker; widening
  *mutation* to `visible_to` lets anyone rename anyone's tag. The non-writable case answers
  404, not 403, so probing cannot enumerate other accounts' tags.
- **Tags are per-owner; the shared tier is `user_id IS NULL`.** `resolve_or_create_tag` takes a
  **required** `user_id` — an ownerless row is a *system* tag, published to every account, and
  only `initial_data._ensure_default_tags` may create one. Lookup prefers the caller's row, then
  a same-named system row, so applying a seeded default attaches the shared row. `POST
  /tags/promote` (admin) moves an owned tag into that tier and folds same-named rows into it.
  Names are unique only **per owner**, so `remove_tag_from_file` resolves via `FileTag` for that
  file, never by name.
- **One file never carries the same word twice.** `resolve_or_create_tag(..., file_id=...)`
  consults `lookup_tag_on_file` first, so the second person to tag a shared file reuses the row
  instead of forking a same-named one. Without it the detail page renders the tag twice and the
  gallery's ALL-filter has to count `DISTINCT Tag.name` to compensate.
- **A tag share grants vocabulary, not administration** (`v386_add_tag_share`). `tag_share`
  mirrors `collection_share` — one user or one group, CHECK-constrained, partial unique indexes —
  but deliberately has **no permission column**: the recipient can see, filter by and apply the
  tag while rename/merge/delete stay with the owner, so a `viewer`/`editor` distinction would be
  a field pretending to be a choice.
- **Bulk tag paths must stay batched.** `prepare_upload.add_tags_to_file` goes through
  `resolve_or_create_tags` (constant SELECTs, issue #284 A2.8), not the per-name resolver;
  `test_upload_prep_batching` fails if the per-name loop returns.
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
