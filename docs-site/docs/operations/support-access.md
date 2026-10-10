---
sidebar_position: 10
title: Support Access & Break-Glass
description: How platform staff reach a tenant's content in a multi-tenant deployment, and the TENANCY_MODE setting that controls it
---

# Support Access & Break-Glass

A platform administrator (`admin` or `super_admin`) manages the deployment: accounts, tasks,
settings, maintenance. In a **multi-tenant** deployment that role does **not** include reading or
changing a tenant's transcripts, files, speakers or collections. Staff reach tenant content only
through a **support-access grant**: time-boxed, scoped to one tenant, approved by that tenant, and
recorded in a use log the tenant can read.

A single-tenant install (no organizations) behaves exactly as before: administrators keep their
instance-wide access and none of the routes on this page exist.

## Which mode am I in?

`TENANCY_MODE` (environment variable, default `auto`):

| Value | Behavior |
|---|---|
| `auto` | Multi-tenant as soon as one active organization exists. A failed detection query never switches the mode on by itself. |
| `multi` | Always tenant-isolated, even with no organization (every personal workspace is a tenant). |
| `single` | Restores the old instance-wide administrator access. If organizations exist, every cross-tenant access is written to the audit log (`platform_admin.content.access`). Use it only when the operator is the sole tenant. |

An unrecognised value fails closed to `multi`. The resolved mode is logged at startup and reported
as `tenancy_mode` on `GET /api/system/capabilities`.

## Requesting access

An administrator asks for a grant (`POST /api/support-access/grants`) naming exactly one target,
an organization or a user's personal workspace, with:

- an access level: `read` or `write`;
- a reason of 10 to 2000 characters;
- a duration of 15 to 480 minutes.

The request is **pending** for 72 hours, then **lapses**. The tenant's organization admins (or the
person, for a personal workspace) are notified and see who is asking: the requester's name and email.

## Deciding

- The organization's admins decide at `/api/org-admin/support-access`; a person decides for their
  own workspace at `/api/users/me/support-access`.
- An approver may **shorten** the requested duration but never extend it.
- Approval and denial are atomic. If two approvers race, or a revoke lands first, the loser gets
  `409 support_grant_already_decided`.
- A platform administrator cannot approve another platform administrator's request, even while
  holding `org:admin` in that organization: the tenant decides, not other staff.
- The tenant (or the grantee, or any `super_admin`) can **revoke** at any time. Revocation is
  idempotent.

## Using a grant

The client sends `X-Support-Access-Grant: <grant uuid>` on each request. A grant is never ambient
and is re-read on every request, so revocation, expiry, demotion of the grantee, or deactivation of
the target take effect on the next request.

- It works only for its grantee, only while **active**, and only inside its tenant.
- A `read` grant refuses every non-safe HTTP method (`403 support_grant_write_required`).
- An organization grant makes the request act inside that organization **without** `org:admin`
  rights; it never confers member management or erasure.
- Every request made under a grant writes a row to the **use log** before it is served: the route,
  and for each tenant resource touched its type, uuid and whether it was read or written. The log
  holds ids only, never filenames or titles. If the row cannot be written the request is refused
  (`503 support_access_audit_unavailable`), so nothing is served unrecorded.
- Transcripts are masked by the **file owner's** redaction policy, not the staff member's own
  preferences, and unredacted reveal stays owner-only.
- Presigned media URLs minted under a grant live at most 300 seconds.

### What a grant never allows

Under a grant, these answer `403 support_grant_action_not_permitted` (the attempt is still
recorded): creating tenant content (uploads, URL ingestion, collections, tags, speaker profiles,
watch sources, shares), chat and search, every download and export, and every `/api/admin` route.

## Break glass

A `super_admin` can open access immediately (`POST /api/support-access/grants/break-glass`) with a
reason, a **ticket reference** and at most **240 minutes**. No approval is needed, so the tenant is
notified in real time, the event is audited, and the grant appears in the tenant's list as
`break_glass`. Use it for outages, not routine support.

## What survives, and the limits

- Grants and their use rows are evidence. Their foreign keys are `SET NULL`, so erasing a tenant or
  a staff account leaves the row in place; the grant becomes permanently unusable. Whether keeping
  the free-text reason after an erasure is acceptable is awaiting legal confirmation.
- Revocation ends API access on the next request. Three residuals remain: a presigned URL minted
  just before revocation (at most 300 seconds), a request already in flight, and Celery work
  dispatched under a `write` grant, which runs to completion.
- Operator runbooks that act on a file by uuid (`/api/files/{uuid}/force`, per-file recovery) need a
  grant on a multi-tenant install. Instance-wide maintenance routes (stuck-file sweeps, backfills)
  are unchanged. `TENANCY_MODE=single` restores the old behavior if the operator is the only tenant.

See also: `docs/security/tenant-boundaries.md` for the full boundary table.
