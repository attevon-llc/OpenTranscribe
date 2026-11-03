# auth — admission control: who gets an account at all (`v381`)

Moved intact from `backend/app/auth/CLAUDE.md`. Read before touching group allow/block lists, pending approval or `auth_admission`.

## Admission control — who gets an account at all (`v381`)

Authentication answers "are you who you say you are". Admission answers "does this deployment
want you". They were the same question for OIDC, which is how JIT provisioning ended up
minting an account for **every identity in a corporate tenant** on first login.

| Control | Applies to | Empty/off means |
|---|---|---|
| `ldap_user_groups` → `ldap_directory._check_group_access` | LDAP | no group requirement |
| `oidc_allowed_groups` / `oidc_blocked_groups` → `oidc/admission.py` | OIDC | admit everyone |
| `require_account_approval` → `auth/approval.py` | self-registration **and** every JIT path | accounts are usable immediately. If the stored setting cannot be read (DB error), `approval_required` returns `None`: new accounts are created `pending` and a pending user gets 503 `auth_config_unavailable` until reads recover |

- **Semicolons delimit both group lists**, matching `ldap_directory._parse_group_list`: a group value
  brokered from a directory is a DN and contains commas. Matching is case-insensitive exact.
- **An empty allow-list admits everyone.** Reading it as "admit nobody" would lock out every
  existing OIDC deployment on upgrade. Only a non-empty list restricts.
- **Blocked means denied**, not "exempt from the allow-list", and is evaluated first.
- The check runs at the **top of `sync_oidc_user_to_db`** — before the create branch and before
  the email-match link. Creating first would leave a row behind for a refused identity;
  linking first would hand one a foothold on an existing account. It re-runs on **every** login,
  so removing someone from the group locks them out rather than only affecting new users.
- Refusals return the **same generic 401** an unusable token gets
  (`provisioning.LINK_REFUSED_DETAIL`) — a distinct message is an account-existence oracle. The
  reason goes to the audit log as `AUTH_LOGIN_FAILURE` / `OIDC_ADMISSION_REFUSED`.
- `approval_status` ∈ `pending`/`approved`/`rejected` is **not** `is_active`: deactivation
  revokes an account that was once usable, approval gates one that never has been. Enforcement
  is a lifecycle gate in `api/endpoints/auth/dependencies.py` (`detail.code ==
  "account_pending_approval"` / `"account_rejected"`), not a second mechanism. Turning the
  setting off releases pending accounts; **rejected stays rejected**, and rejection never
  deletes the row. Admin queue: `GET/POST /api/admin/user-approvals` (**admin** tier — managing
  users; the switch that creates the queue is auth config, hence super_admin).
- **The bootstrap super_admin is never pending** (`initial_data._ensure_admin_user` writes it
  explicitly). Only a signed-in administrator can clear the queue.
