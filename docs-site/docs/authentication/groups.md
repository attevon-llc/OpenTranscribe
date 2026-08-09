---
sidebar_label: IdP group mapping
sidebar_position: 5
---

# IdP group mapping

Both directory paths already carry the caller's full group list — `memberOf` for LDAP, the
configured roles claim for OIDC — and until v0.5.0 everything but a single "is this an admin"
bit was discarded. **Group mapping** is what consumes the rest: a mapping binds one claim value
to an in-app group, to a role grant, or to both.

Added by migration `v378_idp_group_mapping`.

:::note Admin panel covers LDAP and OIDC
Settings → Authentication → **Group mappings** (`GroupMappingSettings`) lets a super_admin
create, edit, delete and dry-run-test LDAP and OIDC mappings without touching the API. It offers
only `ldap`/`oidc` as the source, so a **`proxy`-sourced mapping still has to be created through
the API** below — everything else on this page (the login-time and sweep-time application, the
role cap, the manual-membership protection) is live regardless of how a row was created.
:::

## What a mapping is

| Field | Meaning |
|---|---|
| `source` | `ldap`, `oidc` or `proxy` — which directory's/assertion's claims this mapping matches |
| `claim_value` | The group DN or role/group name the provider asserts (max 1024 chars) |
| `group_uuid` | An existing in-app `UserGroup` to place the user in (optional) |
| `grants_role` | `user` or `admin` (optional) |
| `description` | Free text (optional) |

A mapping must grant *something* — a group, a role, or both. That is checked in the Pydantic
schema, in the service, and by a database CHECK constraint, because each layer is reachable
without the others.

### `super_admin` is unreachable from any identity provider

`grants_role` is capped at `admin`. This is enforced three times over: a regex on the wire
contract, `assert_grantable_role()` before anything is persisted, and the
`ck_group_mapping_role_capped` CHECK constraint on the table.

A `super_admin` account is also **never demoted** by directory reconciliation, in either
direction. It is the break-glass account for the directory that might be the thing that is
broken.

### Claim comparison

- **LDAP** distinguished names are compared **case-insensitively**, and a partial unique index
  (`uq_group_mapping_ldap_claim_ci`) keeps the table from holding two mappings that differ only
  in case.
- **OIDC and proxy** role/group strings are opaque, case-sensitive identifiers and are compared
  verbatim. Folding them would silently merge `Legal` and `legal`.

## How mappings are applied

One implementation, two callers — there is deliberately no second copy, because a login-only
version would never revoke and a sweep-only version would leave a freshly-promoted user waiting
a day for their groups.

| Caller | When | Sources |
|---|---|---|
| Login | Every LDAP, OIDC and trusted-header (proxy) sign-in | `ldap`, `oidc`, `proxy` |
| [Directory sync](./ldap#directory-sync-and-deprovisioning) | On the configured schedule | `ldap` only |

A proxy sign-in only reconciles membership when the proxy actually forwards a groups header
(`PROXY_GROUPS_HEADER`) — an **absent** header means "I do not manage your groups" and skips
reconciliation entirely, while an **empty** one reconciles to empty. See
[Trusted-header setup](./proxy).

For each pass:

1. Resolve the user's asserted claim list against the mappings for that source.
2. **Add** membership in every mapped group the user is not already in, marked
   `source = <directory>`.
3. **Remove** every directory-sourced membership whose group is no longer in the resolved set —
   including when the mapping itself was deleted.
4. Apply the role: promote to `admin` if any matched mapping grants it (OR-ed with the legacy
   `ldap_admin_users` / `ldap_admin_groups` / `oidc_admin_role` signal), demote `admin` → `user`
   if nothing does.

**A privilege change revokes that user's sessions** and is audited as an `ADMIN_ROLE_CHANGE`
whose actor is the directory (`idp_login` or `directory_sync`) rather than a person.

### Hand-added memberships are untouchable

Only rows whose `source` is a directory are ever removed. A `manual` membership survives every
pass, is never removed, and is never converted — a mapping that would duplicate one leaves it
manual. If an admin put someone in a group by hand, that decision outlives the directory.

`user_group_member.source` defaults to `manual`, so the default *is* the backfill: every
membership that existed before `v376` stays hand-managed.

### Upgrading changes nothing on its own

With no mappings configured, `resolve_grants` returns an empty result and the pass is a no-op:
no membership changes, and the legacy admin signal alone decides `admin`, exactly as before.

### OIDC is login-time only

There is no provider-neutral "list all users" primitive for OIDC, so there is no periodic OIDC
sweep. An OIDC user's groups and role are reconciled when they sign in. LDAP gets both.

## API

All routes are `super_admin` and live under `/api/admin/group-mappings`.

| Method | Path | Purpose |
|---|---|---|
| `GET` | `/api/admin/group-mappings` | List, optionally filtered by `?source=` |
| `POST` | `/api/admin/group-mappings` | Create |
| `PUT` | `/api/admin/group-mappings/{uuid}` | Update (all fields optional) |
| `DELETE` | `/api/admin/group-mappings/{uuid}` | Delete |
| `POST` | `/api/admin/group-mappings/test` | Dry-run a resolution — writes nothing |

Create:

```bash
curl -X POST https://yourdomain.com/api/admin/group-mappings \
  -H 'Content-Type: application/json' \
  -H "X-CSRF-Token: $CSRF" --cookie "$COOKIES" \
  -d '{
        "source": "oidc",
        "claim_value": "opentranscribe-admins",
        "grants_role": "admin",
        "description": "Platform administrators from the IdP"
      }'
```

```bash
curl -X POST https://yourdomain.com/api/admin/group-mappings \
  -H 'Content-Type: application/json' \
  -H "X-CSRF-Token: $CSRF" --cookie "$COOKIES" \
  -d '{
        "source": "ldap",
        "claim_value": "CN=Legal-Team,OU=Groups,DC=example,DC=com",
        "group_uuid": "019ec90a-3f41-7aaa-8000-0000000000a1"
      }'
```

### Testing a mapping before you rely on it

`POST /api/admin/group-mappings/test` resolves a claim list — or, for LDAP, a real directory
account — and reports what *would* happen. Nothing is written.

```bash
# By claim values (works for both sources)
-d '{"source": "oidc", "claim_values": ["engineering", "opentranscribe-admins"]}'

# By directory account (LDAP only)
-d '{"source": "ldap", "username": "john.doe"}'
```

The response carries `matched_claims`, `unmatched_claims`, the resolved `groups`, the mapped
`grants_role`, whether the **legacy** admin signal fired (`legacy_admin`), and the
`effective_role` the two combine to. `unmatched_claims` is usually what you want: it is the
list of things the provider asserted that no mapping recognised — typically a typo, or the
case-sensitivity difference between the two sources.

Looking a user up by name is **LDAP-only**. An OIDC provider asserts group membership inside a
token issued to that user, and there is no provider-neutral way to look it up for somebody else;
paste the values the provider emits instead.

## Related

- [Authentication overview → Privilege tiers](./overview#privilege-tiers)
- [LDAP setup](./ldap)
- [OIDC setup](./oidc)
- [Trusted-header (reverse proxy) setup](./proxy)
