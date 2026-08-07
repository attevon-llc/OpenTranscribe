# Complete auth implementation — one branch, all tiers

Supersedes nothing; it **sequences** the work already specified in
`plans/oidc-conformance-plan.md` (OIDC standards conformance) and
`plans/auth-parity-open-webui.md` (R1–R11 capability gaps), plus the remaining items of the
original overhaul plan, into a single delivery on
`security/auth-identity-overhaul`.

**Decision (owner: David, 2026-08-07):** all of it lands in this one branch. I recommended
splitting the new capabilities out; that recommendation was heard and declined. The branch is
already ~163 files / ~25k insertions, so the sequencing below exists to keep it *reviewable*
even though it is large: each phase is independently green, and each phase boundary is a valid
stopping point.

## The target

| Tier | Means |
|---|---|
| **Local / dev** | `./opentr.sh start dev`, no configuration, no mail server. Nothing this branch adds may fire on that path. This is the **acceptance gate for every phase**, not a phase of its own. |
| **Small business** | LDAP or OIDC configured entirely in the admin UI, local passwords and self-registration off, new staff onboarded without hand-written SQL or `.env` edits. |
| **Enterprise** | SSO with admission control, group-driven authorization, SCIM provisioning, directory-driven deprovisioning, MFA, PKI, session lifetime controls, and an audit trail that is visible in the product. |

## Migration ownership

**I own every Alembic revision in this tranche.** Agents must not author one; they consume the
columns. Three revisions, each a coherent transaction:

| Revision | Contents |
|---|---|
| `v376_rename_keycloak_config_to_oidc` | `auth_config` + `auth_config_audit` data rename `keycloak_*` → `oidc_*`, category `keycloak` → `oidc`. Ciphertext carried across **unchanged** — no decrypt/re-encrypt. |
| `v377_oidc_identity_columns` | `user.auth_type` `'keycloak'` → `'oidc'`; drop/re-add `ck_user_auth_type_valid` with `('local','ldap','oidc','pki','proxy')`; same for `ck_user_invitation_auth_type_valid`; `keycloak_id` → `oidc_subject`; `keycloak_refresh_token` → `oidc_refresh_token`; `refresh_token.oidc_id_token`. **Single transaction** — a half-applied state either locks out every OIDC user or exempts them from MFA. |
| `v378_directory_groups_approval_scim` | `group_mapping` table; `user_group_member.source`; `user.approval_status` / `approved_at` / `approved_by`; `scim_token` table. |

Each gets a detection arm at the TOP of `_detect_schema_version()` (`app/db/migrations.py`) and a
consistency test modelled on `test_v375_migration_consistency.py`. **On rebase onto master after
rag-chat merges, v375–v378 renumber to v376–v379** (rag-chat also defines v375) and the detection
arms must be re-keyed.

## Phases

Each phase: backend + admin UI + i18n across all 8 locales + tests + docs. Definition of done for
every phase is the same five gates — `pre-commit run --all-files`, backend unit suite at or above
baseline, `npm run check:i18n && check && test && build`, no new dead config key, and the Tier-1
gate above.

### Phase 0 — close what is already half-built

Driven by the two running audits. Anything returning PARTIAL is a defect of exactly the kind this
whole branch exists to eliminate (a setting validated but never read; an endpoint with no UI), so
it is fixed before anything new is added. Includes the auth-mail designation UI, already in
flight.

### Phase 1 — OIDC conformance and the `oidc_*` rename

`plans/oidc-conformance-plan.md` §3 and §5. Rename the whole surface; `KEYCLOAK_*` env vars keep
working forever as an **input adapter** in `ENV_TO_CONFIG_MAPPING`, with a one-shot deprecation
warning at startup. Routes `/api/auth/keycloak/*` → `/api/auth/oidc/*` (sole consumer is our own
SPA; the IdP redirect URI points at the frontend, so no IdP reconfiguration).

**Test-enforced invariant:** after this phase the string `keycloak` may appear under `backend/app/`
in exactly two files — the env ingestion map and the migration. A unit test greps for it, in the
spirit of the existing `clerk|stripe` seam guard.

**Decided here, not left open** (parity doc §8.2): the ID token is stored **server-side on the
session row**, never in a cookie. RP-initiated logout needs `id_token_hint`; Open WebUI's
`ENABLE_OAUTH_ID_TOKEN_COOKIE` defaults on and their own docs call it unsafe. We take the other
branch, and it dies with the session.

Bump `CLOUD_SEAM_VERSION` — this touches JIT provisioning and `auth/external_sync.py`. Coordinate
with the pinned submodule in the private cloud repo before merge.

### Phase 2 — OIDC admission control (parity R3)

The live security gap. `sync_keycloak_user_to_db` creates an account **unconditionally**; the only
group-ish setting is `keycloak_admin_role`, which *elevates* rather than *admits*. Point this at a
corporate realm and every identity in it is provisioned.

`oidc_allowed_groups` / `oidc_blocked_groups`, evaluated against the claim named by
`oidc_groups_claim`, mirroring LDAP's `_check_group_access`. **Deny is the documented meaning of
blocked** (their docs and code disagree; deny is the useful reading). Empty allow-list = admit all,
preserving current behaviour on upgrade. Refusals are audited.

Lands the claim-extraction code Phase 4 needs.

### Phase 3 — approval state (parity R5)

`user.approval_status` ∈ `pending` / `approved` / `rejected`. A JIT-provisioned or self-registered
account can land `pending`, gated by an auth-config key, with an admin queue in the Users table.
This is the landing state Phases 4 and 5 both want, which is why it precedes them.

The refusal is a distinguishable 403 (`detail.code == "account_pending_approval"`) handled by the
SPA's existing account-lifecycle classifier — **not** a new mechanism.

### Phase 4 — IdP groups → in-app groups (parity R2)

Both `ldap_auth.py` and the OIDC path already extract the full group list and then throw away
everything but `is_admin`. `group_mapping` maps a claim value onto an existing `UserGroup`, with an
optional `grants_role` capped at `admin` (`super_admin` stays local-only).

`user_group_member.source` marks directory-derived membership, which is what makes revocation
tractable: reconciliation removes only what it added, never a hand-managed membership.

### Phase 5 — trusted-header / reverse-proxy auth (parity R1)

`auth_type='proxy'`. **Generalise the existing PKI header machinery — do not fork the trust
check.** `pki_mode='header'` becomes a specialisation of it (a proxy asserting a subject DN rather
than an email), per the repo's "delete the old one" rule.

Strictly better than the reference implementation, deliberately:
fail-closed CIDR allowlist (empty allowlist refuses every assertion; `main.py` refuses to boot
hardened), an optional constant-time shared secret, a role header that is opt-in and capped at
`admin`, a normal `refresh_token` session so every existing control applies unchanged, a
per-request consistency check, and every assertion audited including refusals.

### Phase 6 — SCIM 2.0 (parity R4)

RFC 7643 / 7644. `/scim/v2/Users` and `/scim/v2/Groups`, bearer-token authenticated against a
hashed `scim_token` row, super_admin-issued and revocable. Writes flow through the same services
the UI uses, so provisioning cannot bypass the role cap or the invariants.

Sized honestly: the `PATCH` path-operation surface is where this becomes M vs XL, and Okta and
Entra exercise different subsets. Ships with whatever subset is verified, and says which.

### Phase 7 — first-run setup flow (parity R8)

Deliberately last: it is a *presentation* of Phases 1–6 plus the existing tabs, so building it
earlier means building it twice. Detects a fresh install, walks super_admin creation → identity
source → mail transport → policy, and never blocks the zero-configuration local path.

### Phase 8 — documentation, CHANGELOG, verification

`docs-site/docs/authentication/{overview,ldap,pki}.md` and `user-guide/admin-panel.md` are
currently **untouched** by this branch despite the identity-source model, privilege tiers,
invitations, session controls and MFA enrolment all changing. Plus a new `proxy.md`, `scim.md`,
and a rewritten `keycloak.md` → `oidc.md`.

Then the live-stack runbook in `plans/auth-overhaul-verification.md`, which needs a human: v375+
alter the live `user` table and create several tables.

## Phase 9 — documentation rebuild (owner: David, explicitly requested)

Runs **after every auth phase is complete**, not interleaved, so it documents the shipped
behaviour once rather than chasing it.

Surface: **51** Docusaurus pages, **70** files in `docs/`, **28** READMEs, **44** `CLAUDE.md`.

1. **Docusaurus must build and serve.** `docs-site/node_modules` is currently absent, so nobody
   has built it recently. `npm run build` must pass, and the docs container must serve at `/docs/`
   with the right `baseUrl` (`DOCS_BASE_URL`, default `/`) — the reporter hit a baseUrl error, and
   the fix (`133efc77`) needs re-verifying against the final state. Check for broken internal
   links; Docusaurus fails the build on them, which is the gate we want.
2. **Auth pages rewritten to match reality**: `authentication/{overview,ldap,pki}.md` and
   `user-guide/admin-panel.md` are **untouched** by this branch despite the identity-source model,
   privilege tiers, invitations, session controls, MFA enrolment and banner all changing. Plus new
   `proxy.md`, `scim.md`, `groups.md`, and `keycloak.md` → `oidc.md`.
   `docs-site/docs/features/authentication.md` needs the same treatment.
3. **`docs/` needs a cull.** It carries a large amount of superseded planning prose —
   `ProjectPlan.md`, `FORK_IMPLEMENTATION_PLAN.md`, `FRONTEND_AUTH_IMPLEMENTATION_PLAN.md`,
   `RELEASE_PLAN_v0.4.0.md`, `E2E_TEST_EXPANSION_PLAN.md`, `SPEAKER_PROFILE_FIX_PLAN.md`,
   `IMPLEMENTATION_AUDIT_REPORT.md`, `DOCUMENTATION_IMPLEMENTATION_SUMMARY.md`,
   `DECEMBER_2025_INTEGRATION.md`, and three overlapping FIPS documents. **Propose the delete
   list before deleting** — some may still be referenced.
4. **READMEs and `CLAUDE.md`**: every `CLAUDE.md` whose subsystem this branch changed must be
   updated *in that file*, not in the root one. The root `CLAUDE.md` holds only what applies
   everywhere.
5. **`plans/` does not ship.** ~3000 lines of planning prose (this file included) is a working
   artefact. Decide before merge whether it belongs in the release branch.

## Non-negotiables carried through every phase

- **`role` is the sole authorization truth**; `is_superuser` is its derived mirror. External IdPs —
  including SCIM and the proxy header — grant at most `admin`. `super_admin` is local-only, because
  it is the break-glass account for exactly the IdP that is failing.
- **Fail closed.** Gate on `settings.is_hardened`, never `ENVIRONMENT == "production"`.
### No dead surface — test-enforced, not aspirational

This is the branch's whole reason for existing, and the audits proved good intentions are not
enough: a feature that is 90% built and 0% reachable passes every test in the suite, because tests
assert that *storage round-trips*, not that *behaviour changed*. Instances found so far — every one
of them shipped green:

| Shape | Example |
|---|---|
| Setting written, never read | 30 of 83 auth-config keys |
| Column enforced, never written | `user.account_expires_at` |
| Column written, never read | `must_change_password` before this branch |
| Helper with zero call sites | `DynamicAuthSettings.password_*`, added *by this branch* |
| Event type with zero emitters | `AUTH_SESSION_LIMIT_EXCEEDED`, `ADMIN_USER_DELETE` |
| Endpoint with no UI | directory sync, auth-mail designation |
| UI with no endpoint | banner acknowledgment before this branch |
| Schema key that is an orphan alias | `mfa_issuer`, `password_require_numbers` |
| Dead UI branch + its stale i18n | `backendNotReady` "coming soon" |

So the guard is a **gate, not a review habit**. A `backend/tests/unit/` suite that fails CI when:

- a key in `CONFIG_CATEGORIES` has no reader outside the config plane and is not in
  `RESTART_REQUIRED_KEYS`;
- an `AuditEventType` member has no emitter;
- a nullable user/session column with an enforcement site has no writer;
- a mounted route has no caller (frontend call site, SCIM/OIDC contract, or a documented
  integration) — the allow-list of deliberate exceptions carries a **reason string**, like the
  existing `KNOWN_PUBLIC` set in `test_route_privilege_tiers.py`.

Precedent already in the repo: `test_every_capability_has_an_audience` and
`test_capability_contract.py`. Extend that pattern; do not invent a second one.

**Definition of done for any feature in this plan: a test that fails if the wiring is removed.**
Not "the value is stored" — "the behaviour changes". Where something genuinely cannot be wired
(frozen at import), it is marked `requires_restart` and shown as such in the UI. A visible
"requires restart" badge is an acceptable outcome; a silent no-op with a success toast is not.
- **One implementation per rule.** No second trust check, no second session store, no second
  group-membership writer.
- **Anti-enumeration holds.** Nothing added here may make an existing address distinguishable from
  an unknown one by status code, timing, or message.
