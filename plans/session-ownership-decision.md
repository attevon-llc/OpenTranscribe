# Decision: `RefreshToken` is the single owner of a session

**Status:** decided, pending implementation.
**Supersedes:** the "wire up `SessionManager`" option left open by the auth audit.

## The problem

Two session mechanisms exist and only one of them runs.

`app/auth/session.py:SessionManager` is ~250 lines of working, Redis-backed code implementing
idle and absolute timeouts (`create_session`, `validate_session`, `invalidate_all_user_sessions`,
256-bit ids, format validation). It is instantiated as a module singleton at the bottom of the
file and **imported by nothing** — grep finds no call site, and no test references it. So
`SESSION_IDLE_TIMEOUT_MINUTES` and `SESSION_ABSOLUTE_TIMEOUT_MINUTES` are configuration that
changes nothing, and refresh rotation has **no absolute cap**: a continuously-active client holds
an indefinitely renewable session.

Meanwhile `RefreshToken` (Postgres) is already the de-facto session record:

- `login.py` counts rows to enforce `MAX_CONCURRENT_SESSIONS` and evicts the oldest.
- `token_service.rotate_refresh_token` rotates one per use and revokes its predecessor.
- `revoke_all_user_tokens*` revokes them, and the fail-closed revocation fallback added for
  issue #324 answers *from these rows* when Redis is unavailable.
- The per-user revocation epoch added in this branch extends that reach to stateless access
  tokens.

## Decision

**`RefreshToken` owns sessions. `SessionManager` is deleted.**

Reasons, in order of weight:

1. **Two owners would disagree.** `max_concurrent_sessions` counts Postgres rows; idle timeout
   would count Redis entries. The moment they diverge — a Redis eviction, a failover, a replica
   with a cold cache — the two controls enforce against different session sets, and neither is
   obviously wrong. One record or the controls are not composable.
2. **Redis is explicitly not the system of record here.** Issue #324 already established that and
   rewrote revocation to consult Postgres when Redis is unreachable. Putting session lifetime on
   Redis re-introduces exactly the dependency that work removed.
3. **Durability and replica-sharing come free** with the table; `SessionManager`'s in-memory
   fallback is per-process and unshared, which is the failure mode that made revocation silently
   stop working before #324.
4. **The wiring cost is real.** Adopting `SessionManager` means embedding a `sid` in every JWT and
   creating a session at five separate mint sites (`login`, `keycloak`, `pki`, `mfa`, `refresh`) —
   more surface, more to keep in sync, no capability the table cannot provide.

The repo's own rule applies: *if you replace an implementation, delete the old one — never leave
two paths doing the same job* (`CLAUDE.md`). Leaving 250 lines of dead session code in the auth
package is how this ambiguity arose.

## Implementation

Two columns on `RefreshToken` (in the existing unshipped `v375`, not a new revision):

- `last_activity_at` — set on every rotation.
- `absolute_expires_at` — set once at first issue and **carried forward unchanged** through every
  rotation. This is what caps a session that refreshes forever.

Enforced in `token_service.verify_refresh_token`, alongside the existing revoked/expired checks:

- reject when `now > absolute_expires_at` → absolute cap;
- reject when `now - last_activity_at > SESSION_IDLE_TIMEOUT_MINUTES` → idle cap.

Both settings become DB-backed through `DynamicAuthSettings` like the rest of the auth config, so
the admin Session tab stops being inert.

### The granularity caveat, stated honestly

Enforcing idle timeout **at refresh** means the check fires once per access-token lifetime
(60 min by default), not per request. That is deliberate:

- Per-request activity tracking is what the audit warned about — polling endpoints (progress,
  notifications, task status) and WebSocket keepalives would refresh `last_activity_at`
  continuously, so idle timeout would *appear* to work and never actually fire. That is worse
  than not having it, because it reads as a satisfied control.
- "Has this client refreshed within the idle window?" is a faithful proxy for "is this session
  idle?", and the error is bounded by the access-token lifetime.

If a future requirement needs true per-request idle timeout (FedRAMP AC-11 at a stricter reading),
it needs an explicit non-activity denylist for the polling routes, and that should be its own
change with its own review — not a side effect of this one.

## Consequences

- The Session tab in Settings → Authentication becomes real for `session_idle_timeout_minutes`,
  `session_absolute_timeout_minutes` and `max_concurrent_sessions`.
- `concurrent_session_policy` still needs its vocabulary fixed — the UI offers
  `oldest`/`newest`/`all` while the backend compares against `reject`/`terminate_oldest`, so no UI
  value can ever match. Fix the UI to the backend's values; the backend's are the ones with code
  behind them.
- `jwt_access_token_expire_minutes` / `jwt_refresh_token_expire_days` stay **env-only and
  restart-required** — they are read at import time by `cookies.py` to compute cookie max-age.
  Mark them `requires_restart` (the column exists and has never been written) rather than
  pretending they are live.
- Existing sessions have NULL in both new columns. Treat NULL as "no cap recorded" and stamp it on
  first rotation rather than invalidating every session on upgrade — users are already being
  signed out once by the token-type change, and twice is gratuitous.
