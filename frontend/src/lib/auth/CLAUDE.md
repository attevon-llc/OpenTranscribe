# src/lib/auth — client-side auth flows

## Purpose

Pure (or near-pure) rules for sign-in and session handling. The session itself is an httpOnly cookie
the SPA cannot read; the SPA learns state from `GET /auth/session` and `GET /auth/methods`.

## Key files

- `accountLifecycle.ts` — classifier for the 403s whose `detail` is `{code, ...}` (account
  lifecycle refusals); the store-side hold lives in `$stores`.
- `authMethods.ts` — `/auth/methods`: the fail-closed fallback when the probe fails and the shared
  short-lived request every caller on a page load joins.
- `authErrors.ts` — user-facing results for failed login / registration / password change / PKI /
  MFA requests.
- `sessionProbe.ts` (bounded, visibly-retrying probe that gates first render), `sessionRecheck.ts`
  (what to do when the server closes the realtime socket because sessions were revoked),
  `sessionTimeouts.ts` + `idleGuard.ts` (idle/absolute timeout for externally-owned sessions, #1106).
- `oidcLogin.ts`, `oidcCallback.ts` (nothing in it may throw), `oidcRedirect.ts` (validates the IdP
  URL before the browser is sent to it), `externalSignIn.ts` (cloud-edition sign-in; a no-op in the
  community build), `loginBanner.ts` (FedRAMP AC-8), `loginIdentifier.ts` (email vs username).

## Conventions / patterns

- Keep these provider-agnostic and side-effect-light; callers own navigation and toasts.
