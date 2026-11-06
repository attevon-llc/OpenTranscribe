# LDAP / Active Directory authentication

OpenTranscribe can verify passwords against an LDAP directory or Active Directory, next to
local accounts and the other identity sources (OIDC, SAML, PKI, trusted proxy). Each account
is owned by exactly one source, recorded in `user.auth_type`. The first successful directory
login of a person creates their account with `auth_type='ldap'`; later logins refresh it.

The maintained reference for the same feature lives on the documentation site:
[LDAP / Active Directory](../docs-site/docs/authentication/ldap.md). This page is the
operator's guide: how to configure it, what each setting does, and how to read what the
backend logs.

## Where it is configured

A super_admin configures LDAP in the admin UI under **Settings → Authentication →
LDAP/Active Directory**. Saved values apply to the next login, with no restart.

Values come from two places, per key:

1. the value saved in the admin UI (the `auth_config` table, category `ldap`), when there is
   one;
2. otherwise the matching `LDAP_*` environment variable from `.env`.

So the environment variables are a bootstrap seed and a per-key fallback; once a key is saved
in the UI, the UI value wins. Of these values only the bind password is encrypted at rest
(AES-256-GCM); the rest are stored as plain configuration. The API never returns the stored
bind password.

The panel has a **Test connection** button. It binds the service account the same way a
login does: it negotiates StartTLS when that is configured and reports a failure instead of
falling back to an unencrypted bind; with neither LDAPS nor StartTLS configured it refuses
before opening a connection and says why; with no bind DN or no bind password it tries an
anonymous bind; and it waits at most the configured `ldap_timeout`. A green result proves the
service account can bind. It does not prove that the search base, the search filter or the
group settings find the right people: try a real login for that.

## Settings

| Admin UI key | Environment variable | Default | Meaning |
|---|---|---|---|
| `ldap_enabled` | `LDAP_ENABLED` | `false` | Turns directory login on |
| `ldap_server` | `LDAP_SERVER` | empty | Host name, or an `ldap://` / `ldaps://` URL |
| `ldap_port` | `LDAP_PORT` | `636` | TCP port |
| `ldap_use_ssl` | `LDAP_USE_SSL` | `true` | LDAPS: TLS from the first byte |
| `ldap_use_tls` | `LDAP_USE_TLS` | `false` | StartTLS: upgrade a plain connection before binding |
| `ldap_bind_dn` | `LDAP_BIND_DN` | empty | DN of the read-only service account used to search |
| `ldap_bind_password` | `LDAP_BIND_PASSWORD` | empty | Its password (the one encrypted value) |
| `ldap_search_base` | `LDAP_SEARCH_BASE` | empty | Subtree that holds the user entries |
| `ldap_username_attr` | `LDAP_USERNAME_ATTR` | `sAMAccountName` | Attribute holding the login name |
| `ldap_user_search_filter` | `LDAP_USER_SEARCH_FILTER` | `({username_attr}={username})` | Filter used to find the user |
| `ldap_email_attr` | `LDAP_EMAIL_ATTR` | `mail` | Attribute holding the email address |
| `ldap_name_attr` | `LDAP_NAME_ATTR` | `cn` | Attribute holding the display name |
| `ldap_timeout` | `LDAP_TIMEOUT` | `10` | Seconds (1-300) for the connect and for every read |
| `ldap_admin_users` | `LDAP_ADMIN_USERS` | empty | Comma-separated usernames that are admins |
| `ldap_admin_groups` | `LDAP_ADMIN_GROUPS` | empty | Semicolon-separated group DNs whose members are admins |
| `ldap_user_groups` | `LDAP_USER_GROUPS` | empty | Semicolon-separated group DNs; when set, only members may log in |
| `ldap_recursive_groups` | `LDAP_RECURSIVE_GROUPS` | `false` | Also count nested group membership (Active Directory) |
| `ldap_group_attr` | `LDAP_GROUP_ATTR` | `memberOf` | Attribute listing the user's groups |

Booleans in `.env` accept `true`/`false`, `t`/`f`, `1`/`0`, `yes`/`no`, `y`/`n` and
`on`/`off`, in any letter case; anything else stops the backend at start-up with a settings
error. That includes `LDAP_RECURSIVE_GROUPS` and `LDAP_TLS_SKIP_VERIFY`. With `LDAP_ENABLED=true` the backend also
refuses to start when `LDAP_SERVER`, `LDAP_BIND_DN`, `LDAP_BIND_PASSWORD` or
`LDAP_SEARCH_BASE` is empty.

## Transport security

There is no unencrypted mode. Every bind, the service account's and the user's alike, goes
over TLS in one of two ways:

- **LDAPS**: `ldap_use_ssl` on, usually port 636. The connection is TLS from the start.
- **StartTLS**: `ldap_use_tls` on and LDAPS off, usually port 389. The backend opens a plain
  connection, negotiates StartTLS, and only then sends the bind. If the connection cannot be
  opened the login fails with the ldap3 error; if the negotiation fails the bind is refused.
  Either way nothing is retried in cleartext, and the connection is closed.

A URL scheme in `ldap_server` decides over the `ldap_use_ssl` switch:

- `ldaps://host` is always LDAPS, whatever `ldap_use_ssl` says, and StartTLS is never
  attempted inside it.
- `ldap://host` is never LDAPS, even with `ldap_use_ssl` on. It needs StartTLS; without it
  every bind is refused, and the refusal message points out the mismatch.
- A bare host name follows `ldap_use_ssl`.

With both switches on and no `ldap://` URL, LDAPS is used and StartTLS is not attempted. With
both on and an `ldap://` URL, StartTLS is used. The backend logs a start-up warning naming
the mode it will use whenever both are on. With neither LDAPS nor StartTLS, every login and
every connection test is refused before anything is sent.

**Timeout.** `ldap_timeout` (default 10 s, 1-300) bounds the TCP connect and each read on the
connection. A directory that accepts the connection and then goes silent does not hang the
login: after that many seconds the bind is refused and the backend logs that the server
did not answer within that many seconds.

Each login opens its own connections (one for the service account, one to check the user's
password) and closes both when it is done; nothing is pooled or cached between logins.

## Finding the user

The search filter has two placeholders:

- `{username_attr}` is replaced with the configured username attribute when the settings are
  loaded, so one filter works for any directory.
- `{username}` is replaced at login with the name the user typed, escaped for LDAP filters
  (backslash, `*`, `(`, `)` and NUL are escaped, so a typed name cannot change the filter).

| Directory | `ldap_username_attr` | Default filter becomes |
|---|---|---|
| Active Directory | `sAMAccountName` | `(sAMAccountName={username})` |
| OpenLDAP, LLDAP | `uid` | `(uid={username})` |
| Custom schema | for example `employeeNumber` | `(employeeNumber={username})` |

A custom filter can narrow the search, for example
`(&(objectClass=person)({username_attr}={username}))`. Curly braces in a filter are read as
placeholders, so a filter must contain no braces other than these two.

**What people type.** Either a bare username or an email address. The text before the first
`@` is searched for with the filter above. Only if that finds nobody is the full text searched
for in the email attribute. If either search matches more than one entry, the login is refused
rather than guessing which account was meant; fix the filter or the duplicate entries.

**Email is required.** The entry must carry a well-formed address (`name@domain.tld`, no
spaces) in the email attribute, or the login is refused. That check runs before the password
is tried.

## Groups and admins

Both group lists hold **full group DNs separated by semicolons**, because a DN itself contains
commas:

```
CN=Transcription Users,OU=Groups,DC=corp,DC=example;CN=Contractors,OU=Groups,DC=corp,DC=example
```

Groups are compared as whole DNs, case-insensitively, after trimming spaces. A partial DN
(`CN=Transcription Users`) matches nothing; copy the exact DN from the directory.

- **`ldap_user_groups`** restricts who may log in at all. Empty means everyone the directory
  authenticates may log in. The check runs before the password is tried.
- **`ldap_admin_groups`** makes members admins. A blank value, or one that holds only
  separators such as `;`, makes nobody an admin.
- **`ldap_group_attr`** names the attribute that lists a user's groups (`memberOf` by default).
  A server that rejects the attribute in a search is asked again without it, so group checks
  then find no direct memberships.
- **`ldap_recursive_groups`** also counts membership through nested groups. It uses Active
  Directory's in-chain matching rule (`1.2.840.113556.1.4.1941`); OpenLDAP and LLDAP do not
  implement that rule, so on those directories it never finds a nested membership.

**Admins.** A directory user is an admin when any of these holds:

- the user is listed in `ldap_admin_users`, a comma-separated list of exact usernames compared
  case-insensitively;
- the user is in one of the `ldap_admin_groups`;
- an IdP group mapping grants admin for one of the user's groups (see
  [Group mappings](../docs-site/docs/authentication/groups.md), page
  `authentication/groups` on the docs site).

The value to put in `ldap_admin_users` is the account's **username attribute exactly as the
directory holds it**. With `userPrincipalName` as the username attribute that is the full
`jane@corp.example`; `jane` alone matches nothing. Logins and the directory sync compare the
same value.

The admin status is recalculated at every login and by every directory sync, so removing
someone from the list or the group demotes them at their next login (or the next sync).
LDAP grants at most `admin`: it never grants super_admin, and a directory login can never take
over a super_admin account.

## Accounts

**First login.** The account is created with `auth_type='ldap'`, the directory's email and
display name (the part of the email before `@` when the entry has no name), no local password,
and the role the admin rules above give it. When account approval is required
(`require_account_approval`), the new account waits as pending until an administrator
approves it. Two first logins arriving at the same moment do not create two accounts.

**Later logins** refresh the display name, and the email only when it differs from the stored
one in letter case or surrounding spaces (that is applied and logged as a security-relevant
change; an empty address never clears the stored one). A genuinely different address is not
applied: the account is found by its directory username, the stored and the asserted email
disagree, and the login is refused with the same answer as a wrong password. The refusal is
logged as `SECURITY: refusing provider-id match ...` and does not heal by logging in again.
After deciding that the rename is legitimate, a super_admin corrects the stored address with
`PUT /api/admin/users/{user_uuid}/external-email`.

**Existing accounts with the same email.** A directory cannot vouch that an email address
belongs to the person logging in, so an LDAP login never attaches itself to an existing
account found only by email. That login is refused with the same answer as a wrong password.
To join a directory identity to an existing account, an administrator links it explicitly
with the link-identity action (`PUT /api/admin/users/{user_uuid}/link-identity`). A local
account linked this way becomes an LDAP account at its next directory login, and its local
password is removed at that moment.

## Passwords

Directory accounts never have a local password, and the per-user local-fallback flag does not
change that. Setting a password in the app, by the user or by an administrator, is refused with
HTTP 400, and the UI does not offer those actions for directory accounts. People change their
password in the directory.

## Security notes

- Use a dedicated, read-only service account for `ldap_bind_dn`.
- The user's password is checked by binding as the DN the search found, never by comparing
  attributes, and only after the email and group checks pass.
- An empty password is refused before anything is sent: on many servers an empty-password bind
  is an anonymous bind and would succeed.
- Typed names are escaped before they reach a search filter.
- No log line contains the service account's password or the user's password.

## Directory sync (deprovisioning)

A periodic sweep (admin API `/api/admin/directory-sync`, configured in the admin panel) asks the
directory about every active LDAP account and reports each one as:

- **present**: groups and admin status are reconciled;
- **absent**: no longer found by the search;
- **disabled**: Active Directory's account-disabled bit is set in `userAccountControl`;
- **not entitled**: no longer in any of the `ldap_user_groups`.

Absent, disabled and not-entitled accounts are deactivated and their sessions revoked, within
the sweep's dry-run and per-run limits. If the directory cannot be reached or the service
account cannot bind, the pass stops and nobody is deactivated. Details are on the docs site
page for LDAP.

## Development and testing

Start the stack with a throwaway LLDAP directory:

```bash
./opentr.sh start dev --with-ldap-test
```

- LDAP on the host at port 3890, LLDAP web UI on port 17170
- Admin login `admin` / `admin_password`, base DN `dc=example,dc=com`

From the backend, use:

| Setting | Value |
|---|---|
| Server | `lldap-test` (a network alias, still valid for `--fresh` stacks) |
| Port | `3890` |
| Use SSL | off |
| Bind DN | `uid=admin,ou=people,dc=example,dc=com` |
| Search base | `dc=example,dc=com` |
| Username attribute | `uid` |

The overlay's LLDAP serves plain LDAP on that port and the overlay sets up no TLS. Because
unencrypted binds are refused, logins and the connection test against it are refused with the
cleartext message until TLS (StartTLS or an `ldaps://` listener) is put in front of it.

Always go through `./opentr.sh`; it loads the overlay into the right compose project.

## Troubleshooting

The backend logs every refusal. Look in `./opentr.sh logs backend`.

| Log message (logger) | Meaning |
|---|---|
| `LDAP login attempted for <name> while LDAP is disabled` (`app.auth.ldap_auth`) | `ldap_enabled` is off |
| `LDAP login refused: empty username or password` | Nothing was sent to the directory |
| `LDAP bind refused before connecting: ...` (`app.auth.ldap_directory`) | Neither LDAPS nor StartTLS applies; the message names the cause (for example an `ldap://` URL with `ldap_use_ssl` on) |
| `LDAP service account bind was refused: check the bind DN, its password and the TLS settings` | The service account could not bind |
| `StartTLS negotiation with the LDAP server failed; bind refused (no fallback to a cleartext bind is attempted)` | The server does not offer StartTLS or refused the upgrade. The connection is closed afterwards |
| `LDAP login for <name> failed: LDAPSocketOpenError: ...` (the type may be a subclass) | The host or port is unreachable, so the socket could not be opened (LDAPS and StartTLS alike) |
| `The LDAP server did not answer within <N> s; bind refused` | The server accepted the connection and then went silent past `ldap_timeout` |
| `LDAP user not found: <name>` | Check `ldap_search_base` and `ldap_user_search_filter` |
| `LDAP filter <filter> matched <n> entries; refusing an ambiguous login` | The filter is too broad or the directory holds duplicates |
| `LDAP login refused for <name>: email attribute ... is missing or invalid` | Populate `ldap_email_attr` for that entry, or point the setting at the right attribute |
| `LDAP user <name> belongs to none of the required groups ...` | Not in `ldap_user_groups`; enable `ldap_recursive_groups` if membership is nested (Active Directory) |
| `LDAP password check failed for <name>` | Wrong password, or the account is locked in the directory |
| `LDAP login succeeded for <name>` | Success |
| `LDAP login for <name> failed: <type>: <message>` / `Unexpected error during LDAP login ...` | A directory or internal error; the type and message say which |
| `SECURITY: LDAP login changes the email of ...` | The directory reported the stored address with different letter case or spacing, and it was updated |
| `SECURITY: refusing provider-id match for ldap identity ... (provider_id_email_mismatch)` | The directory reports a different email for a known account; the login is refused until a super_admin corrects the stored address (see Accounts) |
| `SECURITY: converting local account ... to LDAP; its local password is cleared` | A linked local account had its first directory login |

A local-password login also logs `Falling back to the ORM password check for local user: <name>`
whenever its first verification path does not accept the password, a wrong password included.
That line is routine and says nothing about the directory.

Checks from the backend host:

```bash
# Can the service account search, and what does the entry hold?
ldapsearch -H ldaps://ad.example.com:636 -D "CN=svc-transcribe,OU=Service,DC=corp,DC=example" -W \
  -b "DC=corp,DC=example" "(sAMAccountName=jane)" sAMAccountName mail cn memberOf

# StartTLS on port 389
ldapsearch -H ldap://ad.example.com:389 -ZZ -D "..." -W -b "DC=corp,DC=example" "(uid=jane)"

# Does the server present a certificate the container trusts?
openssl s_client -connect ad.example.com:636 -showcerts </dev/null
```

## Database columns

- `user.auth_type`: which source authenticates the account. It started as a 10-character
  column defaulting to `local`; later migrations widened it and constrained it to the supported
  sources (`local`, `ldap`, `oidc`, `pki`, `proxy`, `saml`, ...).
- `user.ldap_uid`: the value of the configured username attribute for a directory account.
  Unique and indexed.
