# Verification runbook — `security/auth-identity-overhaul`

## Why this is a runbook and not a result

Everything that can be verified without the live stack **has been** (see below). The remaining
steps need the real Postgres/Redis/MinIO, and two of them are your call rather than mine:

1. **Migration `v375` alters the live `user` table** (`role` and `auth_type` → NOT NULL, plus an
   `auth_type` CHECK). It is idempotent and its backfills only ever remove privilege, but applying
   an unreviewed schema change to your live database unattended is not something I'll do.
2. Running the branch means switching the checkout the dev stack bind-mounts, which swaps the
   running backend onto this code.

The worktree deliberately has no `.env` (correctly blocked), so the DB-backed tests cannot run
from it at all.

## Already verified

| Gate | Result |
|---|---|
| `pytest tests/unit` (backend, worktree) | **1184 passed**, 3 failed — all three pre-existing and environmental (2× MinIO `SignatureDoesNotMatch`, 1× Postgres auth; no `.env` here) |
| `ruff check app/` | clean |
| `ruff format --check` | clean |
| `npm run check:i18n` | parity OK, 4084 keys × 8 locales |
| `npm run lint` | 0 errors (554 pre-existing warnings) |
| `npm run check` (svelte-check) | 943 files, **0 errors, 0 warnings** |
| `npm run test` (vitest) | **213 passed** |
| `npm run build` | succeeds; `npm ci --dry-run` exits 0, `prebuild` fetches real assets |

`frontend/node_modules` is still a symlink to the sibling checkout — the one thing not proven
from a clean install.

## Step 1 — move the branch to the main checkout

```bash
cd /mnt/nvm/repos/transcribe-app
git worktree remove .claude/worktrees/authoverhaul   # add --force if node_modules symlink complains
git checkout security/auth-identity-overhaul
```

## Step 2 — start the stack (applies v375)

```bash
./opentr.sh start dev
./opentr.sh logs backend | grep -iE 'v375|migration'
```

Expect `v375_harden_user_auth_invariants` to apply once. **Take a backup first if you want a
rollback point** — `./opentr.sh backup`.

Sanity-check the migration did what it says:

```sql
-- both must be NO
SELECT column_name, is_nullable FROM information_schema.columns
 WHERE table_name='user' AND column_name IN ('role','auth_type');
-- must exist
SELECT conname FROM pg_constraint WHERE conname='ck_user_auth_type_valid';
```

## Step 3 — the full backend gate

```bash
./scripts/run-integration-tests.sh --coverage
```

This is the pre-merge gate: ungated suite, every `RUN_*` security suite in both FIPS modes, and
the integration-marked tests. The DB-backed tests that could not run in the worktree live here —
notably `tests/unit/test_v375_migration_consistency.py`, which replays the backfill against a
deliberately-broken row.

## Step 4 — the one thing no test covers

The account-lockout Redis path was rewritten as a **compare-and-set Lua script** and has only
been exercised against a fake Redis. A Lua error would surface at runtime and silently degrade
every login to in-memory lockout tracking. Confirm it executes against the real Redis:

```bash
# from a nonexistent account — never a wrong password for admin@example.com,
# which poisons the per-account lockout for the whole suite
for i in $(seq 1 3); do
  curl -s -o /dev/null -w '%{http_code}\n' -X POST localhost:5174/api/auth/token \
    -d 'username=nobody-lockout-probe@example.com&password=wrong' \
    -H 'Content-Type: application/x-www-form-urlencoded'
done
./opentr.sh logs backend | grep -iE 'lockout|lua|degraded' | tail
```

Expect 401s and **no** `security_state_degraded_total` increment / no Lua error.

## Step 5 — E2E

```bash
./scripts/e2e/run-e2e.sh
```

Two previously-dead tests were repaired (`test_mfa.py`, `test_pki.py` were selecting a Settings
nav item that does not exist and were masked by `pytest.skip()`), so the MFA UI is genuinely
covered for the first time — watch those specifically.

## Step 6 — browser, light and dark

No agent could do this: the dev stack bind-mounts the sibling checkout, not the worktree. All of
it is new or changed UI.

- **Login page**: password form appears; with `local_enabled=false` **and** `ldap_enabled=false`
  it disappears and only SSO buttons remain; the Register link follows `allow_registration`.
- **Settings → Authentication → Local**: save the panel and confirm success. This one matters —
  the panel now fans out to **four** categories (`local`, `password_policy`, `mfa`, `lockout`)
  because unknown keys are now rejected with a 400. The component test proves the request shapes,
  not the server's acceptance of them.
- **Settings → Authentication → Keycloak**: the Client Secret field must render **empty** with a
  "leave blank to keep current" hint. Then save the panel *without touching it* and confirm LDAP
  and Keycloak still authenticate — the old code re-encrypted the placeholder over the real
  secret on exactly this path.
- **Settings → Users**: the role select offers **Super Admin** to a super_admin; promoting asks
  for confirmation; demoting the last super_admin is refused.
- **Queue Dashboard** (user menu): loads for an admin, is absent for a normal user. Check on
  `start dev`, `start prod --build`, and `start prod --build --with-pki` — the whole point is that
  it 404'd on the last two.
- Every changed screen in **both** light and dark.

## Step 7 — targeted functional checks

- **Identity-source matrix**: for local-only / LDAP-only / Keycloak-only / PKI-only / hybrid,
  confirm the login page renders the right controls and that an active `super_admin` can still
  sign in with a password when `local_enabled=false` (the break-glass rule).
- **OIDC discovery** (`./opentr.sh start dev --with-keycloak-test`): first confirm the existing
  realm-based config still logs in — that is the regression gate — then point
  `keycloak_discovery_url` at that same Keycloak's `.well-known/openid-configuration` and confirm
  login works through the discovery path.
- **PKI** (`./opentr.sh start prod --build --with-pki`): normal login works, and a forged
  `X-Client-Cert-DN` from an untrusted peer is refused. **`PKI_TRUSTED_PROXIES` is now required** —
  a PKI stack without it will authenticate nobody, by design.
- **Forced MFA enrolment**: set `mfa_required`, log in as an unenrolled user, and walk the QR →
  code → backup-codes flow through to a working session.

## Step 8 — security review and merge

```bash
backend/venv/bin/pre-commit run --all-files
/security-review
```

Then open the PR against `master`. `plans/midow-issue-replies.md` holds draft responses for
#353/#354/#355 and the email — **not posted**, since they go to a real user and the wording is
yours.

## Known-open, deliberately not done

Carried from the plan as v0.5.1 candidates, all documented in the audit:

- **P2** — email verification, `must_change_password` + password expiry enforcement,
  `last_login_at` / `account_expires_at` (all three columns are written or declared and read by
  nothing), banner-acknowledgement enforcement, and the `SessionManager` idle/absolute timeout
  decision (Redis vs `RefreshToken` as the single session owner — a design call, not a wiring
  task).
- **P5** — the invitation flow. This is the biggest one: with self-registration disabled, admin
  provisioning is the only path, and it currently mints **local-password accounts only** and
  emails nobody. On a deployment that also disables local auth, it produces accounts that cannot
  log in. Also: no directory deprovisioning (removing a user from AD never disables them here),
  auth email still on the single-SMTP dev service rather than the DB-backed multi-provider one
  already in the repo, and no UI for the session/lock/unlock/MFA-reset admin endpoints.
- Two panels (`mfa.py` at 475 lines, `keycloak_auth.py` at 911) are over the ~300-line guidance;
  both have an obvious seam noted in their reports.
