---
sidebar_label: LDAP / Active Directory
sidebar_position: 2
---

# LDAP / Active Directory

OpenTranscribe authenticates against LDAP or Active Directory alongside its other identity
sources. A directory account is created on first successful login with `auth_type='ldap'`.

Configure it at **Settings → Authentication → LDAP** (super_admin). Values are stored in the
`auth_config` table with the bind password encrypted (AES-256-GCM) and take effect without a
restart; the `LDAP_*` environment variables remain a bootstrap seed and fallback.

## Configuration reference

| Field | Config key | Default |
|---|---|---|
| Enabled | `ldap_enabled` | `false` |
| Server | `ldap_server` | — (`ldaps://ad.example.com`) |
| Port | `ldap_port` | `636` |
| Use SSL (LDAPS) | `ldap_use_ssl` | `true` |
| Use StartTLS | `ldap_use_tls` | `false` |
| Bind DN | `ldap_bind_dn` | — |
| Bind password *(sensitive)* | `ldap_bind_password` | — |
| Search base | `ldap_search_base` | — |
| Username attribute | `ldap_username_attr` | `sAMAccountName` |
| Email attribute | `ldap_email_attr` | `mail` |
| Name attribute | `ldap_name_attr` | `cn` |
| User search filter | `ldap_user_search_filter` | `({username_attr}={username})` |
| Timeout (s) | `ldap_timeout` | `10` |
| Admin usernames | `ldap_admin_users` | — |
| Admin groups | `ldap_admin_groups` | — |
| Required user groups | `ldap_user_groups` | — |
| Recursive group lookup | `ldap_recursive_groups` | `false` |
| Group attribute | `ldap_group_attr` | `memberOf` |

`{username_attr}` in the filter is replaced with `ldap_username_attr`, so the filter stays
correct across directory types:

| Directory | `ldap_username_attr` | Resulting filter |
|---|---|---|
| Active Directory | `sAMAccountName` | `(sAMAccountName={username})` |
| OpenLDAP | `uid` | `(uid={username})` |
| Custom | `employeeId` | `(employeeId={username})` |

Use **Test Connection** before saving.

## Admission control and privilege

Two separate questions, two separate settings:

- **`ldap_user_groups`** — a comma-separated list of group DNs. When set, a user must be a
  member of at least one of them to sign in at all. Empty means "admit everyone the directory
  authenticates". With `ldap_recursive_groups` on, nested-group membership counts too.
- **`ldap_admin_users`** / **`ldap_admin_groups`** — who becomes `admin`.

Group DNs are compared case-insensitively, which is what LDAP itself does.

`require_account_approval` (Authentication → Local) applies here too: with it on, a
directory user provisioned for the first time lands `approval_status = pending` and needs an
administrator to release them from the queue.

Beyond those, [IdP group mapping](./groups) turns any directory group into an in-app group
and/or a role grant, applied at login *and* on the directory-sync sweep. An identity provider
can grant at most `admin`; `super_admin` is local-only.

## Login inputs

Users can sign in with either their directory username or their email address; the login form
is the same one local accounts use, which is why turning off `local_enabled` does not hide it.

```bash
# Directory username
curl -X POST https://yourdomain.com/api/auth/token \
  -H "Content-Type: application/x-www-form-urlencoded" \
  -d "username=john.doe&password=<directory-password>"

# Email address
curl -X POST https://yourdomain.com/api/auth/token \
  -H "Content-Type: application/x-www-form-urlencoded" \
  -d "username=john.doe@example.com&password=<directory-password>"
```

Account lockout is keyed on a **canonical identifier per account**, so a user reachable by both
spellings has one lockout budget rather than two.

## Directory accounts never have a local password

`auth_type='ldap'` is in the "no local fallback" set, and `allow_local_fallback` does **not**
override it — the flag is rejected at write time for LDAP accounts. There is exactly one
implementation of that rule, used by every authentication path.

Consequently LDAP users cannot change their password in OpenTranscribe; that happens in the
directory. The admin password-reset endpoint will not plant a hash on a directory account either.

## Account linking

When a directory login finds no account by `ldap_uid` but *does* find one by email address,
that link is only made when the directory asserts the address is verified, and **never** for a
`super_admin`. A refusal fails the login with the same generic error as a bad credential and is
audited with `error_code ACCOUNT_LINK_REFUSED`. See
[Account linking](./overview#account-linking) for the operator remedy.

## Directory sync and deprovisioning

Before this existed, sync ran only at login and only upward: it created and promoted accounts
and could refuse a login, but **nothing ever disabled one**. An account deleted or disabled in
Active Directory kept a live OpenTranscribe row forever — and because refresh tokens rotate on
every use, an actively-used session survived the user's termination indefinitely.

A periodic sweep now probes every active `auth_type='ldap'` account against the directory:

- **Present** → reconcile [group mappings](./groups) and role.
- **Absent / disabled / no longer in a required group** → set `is_active = false` **and revoke
  every session**. Revocation is the half that actually closes the hole; disabling alone would
  leave the refresh token rotating.

Four rules shape it:

1. **Fail closed on ambiguity, not on error.** "The directory says this user is gone" and "I
   could not ask the directory" are different answers. Only the first one acts; the second
   aborts the pass.
2. **`super_admin` and `local` accounts are never touched** — the first is the break-glass
   account, the second has no upstream identity.
3. **Disable, never delete.**
4. **Bounded and opt-in.**

### Settings

There are **no directory-sync environment variables** and no admin-panel screen yet. The sweep
reads six `SystemSettings` rows; the directory connection itself reuses the LDAP auth config
above.

| Key | Default | Meaning |
|---|---|---|
| `directory_sync.enabled` | `false` | Master switch |
| `directory_sync.schedule` | `0 4 * * *` | Cron (UTC), daily at 04:00 — after backups |
| `directory_sync.dry_run` | `true` | Report what *would* be disabled, change nothing |
| `directory_sync.max_disables_per_run` | `10` | Blast radius per pass |
| `directory_sync.last_run_at` | — | Written by the scheduler |
| `directory_sync.last_result` | — | JSON report of the last pass |

The defaults are deliberately timid: **an operator has to opt in twice**, once to run the sweep
and once to let it act. A directory that answers "gone" for everybody — wrong search base,
wrong group DN — is indistinguishable from mass offboarding, and `max_disables_per_run` is what
stops one bad configuration from disabling the deployment in a single pass. Group-membership
changes are *not* counted against that cap: it bounds deactivation, and a membership change is
recoverable by re-adding the row.

Celery beat runs `directory.sync_check_schedule` every 15 minutes on the CPU queue; when the
cron is due it dispatches `directory.sync_run`. Changing the schedule needs no beat restart.
A Redis lock prevents overlapping passes.

**Recommended rollout**: enable with `dry_run=true`, read `directory_sync.last_result` for a
few days, then set `dry_run=false`.

### Scope

LDAP only. OIDC and PKI have no "list users" primitive without provider-specific admin APIs, so
OIDC group mappings are applied **at login only** — the one genuine capability difference
between the two directory paths.

## Development / testing

```bash
./opentr.sh start dev --with-ldap-test
```

- LDAP server `localhost:3890`
- Web UI `http://localhost:17170`
- Admin `admin` / `admin_password`
- Base DN `dc=example,dc=com`

## Security

- **Use LDAPS** (port 636, `ldap_use_ssl=true`) or StartTLS in production. Verify with
  `openssl s_client -connect ad-server:636`.
- Use a **read-only** service account for the bind DN.
- Filter values are escaped against LDAP injection (`(`, `)`, `*`, `\`, NUL).
- The bind password is encrypted at rest and **never returned by the API** — a sensitive key
  comes back as `null` with an `is_set` flag, so saving the panel cannot overwrite the stored
  secret with a placeholder.

## Troubleshooting

| Symptom | Check |
|---|---|
| "Failed to bind to LDAP server" | Server, port, and service-account credentials |
| "User not found in LDAP" | `ldap_search_base` and `ldap_user_search_filter` |
| "User has no email attribute" | `ldap_email_attr` is populated for that account |
| User authenticates but is refused | `ldap_user_groups` — they are not in a required group. Turn on `ldap_recursive_groups` if membership is nested |
| Nobody becomes admin | `ldap_admin_users` / `ldap_admin_groups`, or use a [group mapping](./groups) and dry-run it with `POST /api/admin/group-mappings/test` |
| Password change refused | Directory accounts have no local password; change it in AD/LDAP |

## Production checklist

- [ ] LDAPS (or StartTLS) with a valid certificate
- [ ] Read-only bind account
- [ ] Configuration saved through the admin UI, **Test Connection** green
- [ ] `ldap_user_groups` set if the directory should also control *admission*
- [ ] Admin privilege granted through `ldap_admin_groups` or a group mapping, verified with the
      mapping test endpoint
- [ ] Firewall allows outbound LDAP from the backend container
- [ ] Directory sync enabled in dry-run, reviewed, then armed
- [ ] `local_enabled` / `allow_registration` set to match "the directory owns identity" — see
      [the identity-source model](./overview#the-identity-source-model)
