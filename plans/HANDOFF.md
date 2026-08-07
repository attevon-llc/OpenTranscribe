# Handoff — auth overhaul branch

For a fresh agent picking this up. Read this first, then `plans/auth-complete-implementation.md`
and `plans/auth-provider-compatibility.md`.

## What this is

Branch `security/auth-identity-overhaul` in worktree
`/mnt/nvm/repos/transcribe-app/.claude/worktrees/authoverhaul`.

It started as three GitHub issues from a Belgian police zone running v0.4.1 behind Authentik +
LDAP ("the LDAP config page doesn't appear", "can't disable self-registration", "Flower unhealthy").
Auditing those found the reports were the visible tip of a structural problem, and the branch grew
into a full authentication overhaul.

**The branch's organising principle, and the thing to protect:** *no dead surface*. A setting that
saves and changes nothing, an endpoint with no UI, a column enforced with no writer, an audit event
with no emitter — each is a bug. The audits found nine distinct shapes of it, two introduced by this
branch itself. Two CI gates now enforce it (`test_auth_config_has_readers.py`,
`test_rate_limited_endpoints_declare_response.py`) and two more are still owed (task #25).

## State

Last commit: `e6740487`. **There is a large uncommitted body of work in the tree** — proxy auth,
SCIM 2.0, the five admin UI panels, admission control, approval state, and the docs rebuild. It is
green (see below) and needs committing as the first action.

Shipped so far: local password auth with policy/history/expiry/lockout/TOTP MFA, LDAP+AD (incl.
nested groups), generic OIDC (discovery, PKCE, ID-token-only validation), PKI/X.509, invitations,
email verification, approval queue, IdP group mapping, session lifetime controls, directory-sync
deprovisioning, audit trail, three-tier RBAC. Migrations v375–v380.

## Environment

An **isolated test stack** runs the branch code: compose project `otfresh-authtest`, own volumes,
NAS overlay never loaded, ports offset by +100. It bind-mounts the worktree and hot-reloads.

- Frontend http://localhost:5273 · Backend http://localhost:5274
- Postgres 5276 · MinIO 5278 · OpenSearch 5280
- Login `admin@example.com` / `password` — throwaway stack, safe to create and delete test users

Run backend tests from `backend/`:

```
POSTGRES_PORT=5276 MINIO_PORT=5278 OPENSEARCH_PORT=5280 \
  /mnt/nvm/repos/transcribe-app/backend/venv/bin/python -m pytest tests/unit tests/api -p no:warnings -q
```

Without those env vars the suite hits the **real dev database**, which is still pre-v375 and will
produce a wall of confusing errors.

Frontend gates, from `frontend/`, **run serially** — concurrent npm invocations crash esbuild here:
`npm run check:i18n` → `check` → `test` → `build`. Note `npm run check` prints `0 ERRORS` and then
exits non-zero on an esbuild teardown deadlock; judge it by the `0 ERRORS` line.

## Rules learned the hard way — these cost hours

1. **Never `git commit` while an agent is running.** pre-commit stashes all unstaged changes, runs
   hooks, then restores. An agent writing during that window makes the restore conflict, and
   pre-commit reports it as *"files were modified by this hook"*, which sends you looking at
   eslint. Every "hook failure" in this session traced to this.
2. **Run the gates yourself before staging** so the commit is a formality, not a negotiation.
3. **Never `git stash` / `reset` / `checkout` while others hold uncommitted work** — a stash is not
   scoped to your files.
4. **Read a file before editing it with a regex.** Two blind `re.sub` calls broke files here (an
   unbalanced paren, a dropped `return`, de-indented asserts). If a scripted edit is unavoidable,
   verify with `ast.parse` immediately after and re-run the affected tests.
5. **The container is a live mirror of the worktree.** A half-finished edit is served immediately.
   Do not diagnose a 500 against a tree an agent is editing — two false alarms came from this.
6. **Type-checking fakes:** the established pattern is a file-scoped
   `# mypy: disable-error-code="arg-type"` with a stated reason. Do not scatter casts at call
   sites, and never widen a production signature to suit a test.
7. **Tests doing schema DDL need `pytestmark = pytest.mark.xdist_group("migration_ddl")`.** They
   take an ACCESS EXCLUSIVE lock; without the group they pass alone and fail in the parallel run.
8. **Never write `assert _detect_schema_version(...) == REVISION`** in a migration test. Use
   `tests/unit/_migration_detection.assert_detected_at_or_after`. The `==` form goes red the day
   the next revision lands; it silently broke v374–v378.
9. **Migration tests must not assert live table state.** Two did (`v375` grandfathering, `v379`
   upgrade) and went red whenever anyone registered. Recreate the pre-revision shape, replay the
   revision, assert the result.

## Invariants — do not break these

- **`role` ∈ {user, admin, super_admin} is the sole authorization truth.** `is_superuser` is a
  derived mirror enforced by a DB CHECK. Never write it directly; go through
  `roles.role_implies_superuser()`.
- **External IdPs grant at most `admin`.** `super_admin` is local-only by design — it is the
  break-glass account for the very IdP that might be failing. Enforced in three places for group
  mapping (service, DB CHECK, and a guard against demotion).
- **Every token carries a `type` claim and every consumer verifies it.** Access/refresh/MFA tokens
  are signed with the same key; `type` is the only thing separating them. Without it the MFA
  half-token is a full session — a complete MFA bypass. That is what closed it.
- **Fail closed:** gate on `settings.is_hardened`, never `ENVIRONMENT == "production"`.
- **Anti-enumeration:** nothing may make an existing address distinguishable from an unknown one by
  status code, timing, or message. Several refusals return byte-identical responses on purpose.
- **Rate-limited handlers must declare `response: Response`** or slowapi raises a 500 for anything
  returning a Pydantic model. This shipped broken and took a container request to find, because
  every unit test strips the decorator before calling the handler.

## How to work — the loop

Everything remaining is execution against written specs. The discovery is done. Follow this loop
and medium reasoning is sufficient:

1. **One task at a time.** Do not run parallel agents; the collisions cost hours in this session.
2. **Read before editing.** Never regex-edit a file you have not read.
3. **After every change**, from `backend/`: `ruff check --fix` → `ruff format` → `mypy` → the test
   command above. Frontend: the four npm gates, serially.
4. **Commit each task separately**, gates green first. The commit message should say *why*, and
   name anything you left undone.
5. **Update the task list** as you go.

### Stop and ask — do not work around these

These replace the judgement a larger model would apply. If you hit one, stop and report:

- A test must be **weakened or deleted** to make something pass. (Adjusting a test that asserts an
  *implementation detail* is fine. Weakening one that asserts *security behaviour* is not.)
- A change would let an external identity provider grant `super_admin`.
- A change would make a refusal message distinguishable between "account exists" and "does not".
- A change removes a `type` claim check on a token.
- You need to modify the live dev database (port 5176) rather than the isolated stack (5276).
- A migration would need to alter a CHECK constraint that another revision in this branch already
  altered.
- The task as written contradicts something in this file.

## Task-specific guidance

Enough detail that these do not require re-derivation.

### #33 Authlib swap — the mapping

Replace, one module at a time, running the OIDC tests after each:

| Ours | Authlib |
|---|---|
| `oidc/discovery.py` fetch + cache | `AsyncOAuth2Client` metadata loading, or fetch + `JsonWebKey.import_key_set` |
| `oidc/flow.py` PKCE + auth URL + token exchange | `AsyncOAuth2Client.create_authorization_url(code_challenge_method="S256")` + `fetch_token` |
| `oidc/claims.py` ID-token verification | `JsonWebToken(allowed_algs).decode(...)` + explicit `iss`/`aud`/`exp`/`nonce` claim options |
| `oidc/endpoints.py` | discovery metadata dict |

Keep `config.py`, `provisioning.py`, `admission.py` untouched — that is our policy layer.

**Do not** use `authlib.integrations.starlette_client.OAuth` with its session middleware. It wants
to own the session; a session here is a `refresh_token` row with idle/absolute timeouts and a
revocation epoch.

**Preserve exactly:** ID-token-only validation with **no** access-token fallback; the asymmetric
algorithm allow-list (no `none`, no `HS*`); PKCE `S256`; issuer and audience verification; the
public/internal URL split (the authorization endpoint must stay browser-reachable while
token/JWKS/logout use the internal URL).

### #34 drop python-jose — the traps

15 call sites; `core/security.py` and `auth/token_service.py` are the spine.

- Token **`type`** binding (access/refresh/mfa) closed a full MFA bypass. Every decode keeps it.
- FIPS mode selects HS512 vs HS256 (`settings.FIPS_VERSION`). Both modes are gated in CI; run
  `RUN_FIPS_TESTS=true` in both.
- The revocation epoch compares `iat` with `<` **not** `<=`, deliberately, so a token minted in the
  same second as the epoch survives — load-bearing for session re-issue after a password change.
- MFA half-tokens carry `mfa_scope` (enroll vs verify) and are single-use via Redis `SET NX`.

Remove from `requirements.txt` and `requirements-ci.txt` only once nothing imports it.

### #35 SAML 2.0 — scope boundaries

Use `python3-saml` or `pysaml2`. **Never** implement assertion parsing or signature validation
yourself — XML signature wrapping has produced critical auth bypasses in Shibboleth and many
commercial SPs.

Build: SP metadata endpoint, ACS (assertion consumer service), SLO, IdP metadata upload/URL in the
admin panel, certificate rotation. Reuse the existing policy layer — admission control, approval
state, `idp_group_mapping_service`, `account_linking`, sessions, audit. Needs a new migration
adding `'saml'` to `ck_user_auth_type_valid`.

Default `SAML_ASSERTS_EMAIL_VERIFIED = False` (same posture as LDAP and PKI) so the
account-takeover guard stays closed.

### #25 the missing dead-surface checks

Two exist. Add, following the same AST style:

- every `AuditEventType` member has an emitter
- every enforced nullable column has a writer (**`account_expires_at` is still enforced with no
  writer anywhere** — no endpoint, no UI, so time-boxed accounts cannot be expressed)
- **every mounted route has a caller** — a frontend call site, a documented integration contract
  (SCIM/OIDC), or an allow-list entry *with a reason string*, following `KNOWN_PUBLIC` in
  `test_route_privilege_tiers.py`. This is the check that would have caught group mappings shipping
  with five endpoints and zero UI.

Each needs a guard-the-guard test: a scanner that matches nothing must not pass everything.

## What is left

Ordered. Full detail lives in the task list; this is the shape.

**First: commit the tree.** Run the gates, then one commit.

**P1 — make what we claim actually work** (#39, #36, #37, #38)
Provider presets (Keycloak/Authentik/Entra/Okta/Google/Generic); show which claims the token
actually carried; the Authentik `email_verified` remedy — *currently documented and
unimplementable*; a real Authentik test container.

**P2 — the library swaps** (#33, #34)
Authlib replaces ~856 lines of hand-rolled OIDC protocol (discovery, PKCE, token exchange,
ID-token validation). Keep `config.py`, `provisioning.py`, `admission.py` — that is our policy and
no library provides it. Use Authlib as a protocol library only; **do not** adopt its Starlette
session integration, because a session here *is* a `refresh_token` row with idle/absolute timeouts
and a revocation epoch. Then drop `python-jose` (unmaintained, has had algorithm-confusion CVEs)
across 15 call sites.

**P3 — SAML 2.0** (#35, approved)
`python3-saml` or `pysaml2`. **Never hand-roll**: XML signature wrapping has produced critical auth
bypasses in Shibboleth and many commercial SPs. Slots in as `auth_type='saml'` reusing the whole
policy layer, so the marginal cost is protocol binding, SP metadata, ACS, SLO and cert rotation.

**P4 — completion** (#25, #28, #40, #20)
Finish the dead-surface gate (the missing check — *every mounted route has a caller* — is the one
that would have caught group mappings shipping with five endpoints and zero UI). First-run wizard.
Providers that cannot put groups in a token (Google Workspace always; Entra above the overage
threshold — must fail **loudly**, not silently empty). Then the final gate: LDAP/Keycloak container
E2E, `run-integration-tests.sh` in both FIPS modes, the rebase (v375–v380 all renumber, since
rag-chat also defines v375), and the GitHub replies.

## Two things needing David, not an agent

1. **The GitHub replies** are drafted in `plans/midow-issue-replies.md` and **not posted** —
   outward-facing, needs his approval. They must disclose that OIDC no longer takes over a
   pre-existing local account by email match unless the IdP asserts `email_verified`, and
   **Authentik hardcodes that false**. That directly affects the reporter.
2. **Whether `plans/` ships.** ~3000 lines of planning prose currently committed. He said delete
   once consumed; most now is.

## Honest state of verification

Verified against a real database and a real browser: migrations v375–v380 apply from scratch;
`tests/unit` + `tests/api` green; the settings nav, OIDC panel, group mapping, and the full
approval loop were exercised in a browser in light and dark.

**Not yet verified:** a real Authentik login (the provider that prompted all of this), a real LDAP
login through the container E2E suite, SCIM against Okta or Entra, and PKI in this branch. The
`--with-ldap-test` and `--with-keycloak-test` containers exist and the suites are written
(`RUN_AUTH_E2E=true`); they have not been run. That is task #20 and it is the difference between
"the code is right" and "we watched it work".
