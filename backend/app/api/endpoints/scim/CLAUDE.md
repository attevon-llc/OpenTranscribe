# api/endpoints/scim — SCIM 2.0 provisioning (`/scim/v2`, RFC 7643/7644)

Moved intact from `backend/app/auth/CLAUDE.md`.

## SCIM 2.0 provisioning (`/scim/v2`, RFC 7643/7644)

Mounted at **root**, not under `/api`: RFC 7644 §3.1 fixes the base path and every connector
appends `/Users` to it. Bearer-token authenticated against a hashed `scim_token` row (`v382`)
that a super_admin issues at `/api/admin/scim-tokens` and can revoke.

- Writes go through `services/scim_service.py` → `account_security_service` /
  `idp_group_mapping_service`, never straight to the ORM. `active: false` and `DELETE` both
  **disable and revoke sessions**; `DELETE` is a soft-disable, because a connector dropping
  someone from its assignment scope must not erase their transcripts.
- **No SCIM call writes a role**, and none may touch a `super_admin` (mirrors `directory_sync`
  rule 2). Group membership is written `source='scim'`, which
  `MEMBERSHIP_SOURCES_PROTECTED` shields from directory reconciliation and vice versa.
- **Not rate limited, deliberately** — the credential is 256 random bits, an IdP bursts
  hundreds of requests from a small egress pool, and a per-IP limit would throttle the tenant.
  No handler there carries `@limiter.limit`, hence none declares `response: Response`.
- **Supported filter**: exactly `<attribute> eq "<value>"` on `userName`/`externalId` (Users)
  and `displayName` (Groups). Anything else is `400 invalidFilter` — a partial filter
  implementation returns wrong answers a client acts on.
- **The `PATCH` surface is closed and documented** in `api/endpoints/scim/patch_ops.py`, with
  the unsupported half named there. Unsupported paths are `400 invalidPath`, never a 200 for a
  change that did not happen.
