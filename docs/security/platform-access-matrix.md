# Platform access matrix

Every way platform staff (`admin`, `super_admin`) can act on behalf of a user or reach a tenant's
content, enumerated from the code and checked by tests (issue #1281). The data is
[`platform-access-matrix.tsv`](platform-access-matrix.tsv); this page explains how to read it and
what it proves. For the boundary rules themselves see [`tenant-boundaries.md`](tenant-boundaries.md)
and `docs-site/docs/operations/support-access.md`.

## The rule being proven

In a multi-tenant deployment (`TENANCY_MODE=auto` with an active organization, or `multi`) a
platform role carries **no implicit access to tenant content**. Content is reachable only through a
support-access grant that is:

- approved by the tenant (or a `super_admin` break-glass opening with a ticket),
- time-limited (15 to 480 minutes, 240 for break-glass), re-read on every request,
- the caller's own, for exactly one tenant, and revocable at any moment,
- recorded in the use log before anything is served (a failed write answers 503).

There is no impersonation or "login as" path: nothing in the code mints a token for another user.
A grant is a per-request header (`X-Support-Access-Grant`) that changes what the staff member's own
request may reach.

## How the matrix is built

`backend/tests/fixtures/platform_access_matrix.py` derives it; nothing is transcribed by hand
except three small classification tables inside that module, each entry with a written reason.

| Kind | Source of truth | Why it cannot rot |
|---|---|---|
| `route` | the live FastAPI route table and each route's dependency tree | `tests/unit/test_platform_access_matrix.py` fails when the committed TSV differs from the derived rows |
| `site` | an AST scan of `backend/app` for every `.allows(`, grant resolution, bypass construction, grant-header read and scope widening | same test; a site no table accounts for is `UNCLASSIFIED` and fails |

Regenerate after a change and **read the diff** (a new row is a new way to reach a tenant):

```bash
backend/venv/bin/python scripts/generate-platform-access-matrix.py --write
backend/venv/bin/python scripts/generate-platform-access-matrix.py --check
```

Self-scoped routes (a signed-in user acting on their own data) and public routes are not
platform-access paths. `tests/unit/test_platform_bypass_route_coverage.py` closes that set: every
route behind the account gate that reads no request context must be listed there with a reason.

## Reading a row

`gate` classifies the route:

| Gate | Meaning | What the grant does |
|---|---|---|
| `content` | tenant content, resolved through `get_current_context` | honoured: the row is loaded, then `bypass.allows(...)` decides on the row's own tenant; lists widen only inside an organization grant's tenant |
| `content-refused` | chat, search, uploads, content creation, every download and export | refused with 403 `support_grant_action_not_permitted` (the attempt is still recorded) |
| `org-admin` | `require_org_admin` on the base context | ignored or refused; a grant never confers `org:admin` |
| `lifecycle` | the base context | never read, so a stale header cannot lock anyone out |
| `platform-op:<tier>:<category>` | gated by the admin or super_admin dependency | see below |

A route can earn a second row, `platform-op:inline:<category>`, when its handler tests the signed-in
user's role itself (`current_user.is_admin` / `.role`, `getattr(current_user, "role")`, `ctx.user`, a
renamed user parameter, or a module-level helper handed the user). The scan runs on **every**
route, so taking the request context does not make such a handler "caller-scoped"; each one needs
an `INLINE_ROLE_ROUTES` entry with a written reason, and a new one is `UNCLASSIFIED` until it has
one. It does not follow a helper two calls deep, a method, or another module; the leak sweep is
the backstop for those.

Platform-operation categories (a route's category is derived, and `UNCLASSIFIED` fails the test):

| Category | What it may do | Proof |
|---|---|---|
| `grant-gated` | acts on a tenant's row or list; takes the request bypass | the chokepoint tests below |
| `deployment-scope` | rebuilds, repairs or sweeps something no single tenant owns (search indices, the embedding model, orphan sweeps); refused unless `ctx.bypass.deployment_scope_writable` (any admin in a single-tenant deployment, a `super_admin` otherwise; a grant never confers it) | structural (handler must ask for the right) and `tests/api/test_deployment_ops_gating.py` |
| `metadata-audited` | returns other tenants' filenames or owners; emits one `platform_admin.metadata.access` event (ids and counts, never names) in multi-tenant mode | structural (handler must call `audit_metadata_access`) and behavioural test |
| `account-admin` | user records, roles, sessions, approvals, identity mapping | sweep: no tenant content in any response |
| `settings` | deployment configuration and credentials | sweep |
| `platform-ops` | indexes, migrations, metrics, maintenance | sweep |
| `state-repair` | pipeline-state repair by id or in bulk; returns status and counts | sweep (see "Decisions to confirm") |
| `takedown-action` | abuse takedown on a named file and the notice record | `tests/test_takedown_quarantine.py`; by design needs no grant |
| `audit-read` | audit events (ids, actions) | sweep |
| `grant-lifecycle` | request, list and read the caller's own grants | `test_support_access_lifecycle.py`: a request is pending until the tenant approves it |
| `self-scoped` | an admin-gated diagnostic that reads only the caller's own rows | route-level filter |

## What the tests prove

`backend/tests/api/test_support_access_matrix.py`, against the real routes with real bearer tokens:

1. **`test_every_chokepoint_demands_a_valid_grant`**, once per chokepoint kind (file, collection,
   speaker, speaker profile, comment, watch source, task, thumbnail). The admin is refused with no
   grant; with a pending grant (`support_grant_not_active`), an expired one
   (`support_grant_expired`), a revoked one (`support_grant_revoked`); with a valid grant for a
   different tenant; with a valid grant for the personal workspace instead of the organization;
   and holding a personal grant on an organization resource. The control: with a valid grant for
   the right tenant the same request is served and a use row names the resource.
2. **`test_a_read_grant_cannot_write_to_any_chokepoint_kind`**: `support_grant_write_required`.
3. **`test_a_platform_admin_without_a_grant_sees_no_tenant_content`**: a sweep. Every
   parameterless GET that a platform role can call, as `admin` and as `super_admin`, over three
   tenants' seeded rows (an organization, another organization, a personal workspace), returns
   none of their content. Routes that cannot be called in-process are counted and capped.
4. **`test_an_org_grant_widens_lists_only_inside_its_tenant`**: an organization grant lists its own
   tenant's rows and nothing from any other tenant; without it the same list shows none.
5. **`test_cross_tenant_filename_listings_are_audited`**: the platform listings that name other
   tenants' files leave exactly one event carrying ids only.

Existing suites keep covering the grant mechanics on the file route:
`test_support_access_enforcement.py` (lifecycle states, wrong grantee, demoted grantee, fail-closed
use log, presign cap, owner-policy redaction) and `test_support_access_lifecycle.py` (approval,
revocation, break-glass).

## Entry points (from the `site` rows)

- `api/deps_context.py::resolve_request_bypass` is the **only** reader of the grant header and the
  only place a grant becomes a bypass; the thumbnail route (optional auth) calls the same function.
- `services/support_access_service.py::resolve_active_grant` validates: the caller's own grant,
  caller still an active admin, active status, live target.
- Every `.allows(` site decides a row; each is classified by the kind of row it guards.
- `sees_all_in_scope` widens a list to every user's rows **inside the active tenant**:
  single-tenant mode, or an organization grant, which assumes that tenant.
- `admin_reveal_allowed` keeps unredacted reveal owner-only in multi-tenant mode.

## Known limits and observations

- **Single-tenant mode is instance-wide by design.** `TENANCY_MODE=single` (or no active
  organization) gives the platform role today's access; with organizations present that is audited
  as `platform_admin.content.access`.
- **Some chokepoints accept a grant only in the helper, not on the representative route.** The
  collection detail routes and the speaker-profile routes do not forward `ctx.bypass` to their
  helper, so a valid grant is refused there (403, fail closed). The helper's guard is proven
  directly in the test and the route is pinned as refusing. If those routes are wired to forward
  the bypass, flip the flag in the test and update this paragraph.
- **A public file's thumbnail needs no authentication**, so it needs no grant either.
- **Work already dispatched under a `write` grant** (Celery) runs to completion after revocation, and
  a presigned URL minted just before revocation lives at most 300 seconds.
- The sweep reaches parameterless GETs. Routes with path parameters are covered by the per-kind
  chokepoint test, the discipline gate (`test_platform_bypass_discipline.py`, no new `is_admin`
  read) and the route-coverage gate.

## Decisions to confirm

These are admin-gated, grant-free and return no content, so the matrix classes them as platform
operations. Each acts on a named tenant row or user, which is why they are listed here:

- `POST /api/tasks/system/recover-task/{task_id}` and `.../recover-user-files/{user_uuid}`: re-dispatch
  a stuck pipeline for a task or user in any tenant, by id.
- `POST /api/admin/files/{file_uuid}/quarantine` and `/release`, takedown notices: act on any
  tenant's file by uuid, deliberately (the operator must always be able to act).
