# frontend/src/components/supportAccess

## Purpose

Support-access grants (issue #1122): a platform admin asks to act inside a tenant, the tenant
approves (or staff break glass), and the admin works inside a time-boxed, audited session. The
backend plan is `docs/design/1122_platform_admin_tenancy_bypass_plan.md`; the UI plan is
`docs/design/1122_support_access_frontend_plan.md`. Everything here is dead UI unless the backend
reports `tenancy_mode === 'multi'`.

## Rules that are load-bearing

- **Fail closed on tenancy.** Gate every entry point on `$capabilities.tenancyMode === 'multi'`
  (explicit equality). The capabilities store itself is fail-open (an unknown key reads as
  enabled), so `isCapabilityEnabled('...')` here would show support access on every single-tenant
  install. Tests pin this in `SettingsModal.supportAccess.test.ts`.
- **The session is tab-scoped.** `stores/supportSession.ts` keeps only `{grantUuid}` in
  `sessionStorage` (`opentr:supportSession`), never `localStorage`: a second tab must not inherit
  a session the user did not start there. It is restored on boot from `GET /support-access/grants/{uuid}`.
- **Header rule** (`$lib/supportAccess/headers.ts`, used by `$lib/axios.ts` and `thumbnailCache`):
  `X-Support-Access-Grant` goes only on same-origin relative API paths while a session is active,
  never on absolute/presigned URLs, and never on the exempt prefixes `/auth/`, `/system/`,
  `/support-access/`, `/org-admin/`, `/users/me/support-access`, `/chat`, `/admin/` (the grant
  lifecycle must stay callable with the session's own identity; search, chat and `/admin` refuse a
  grant at router level).
- **Ordering across a session switch.** Starting: purge tenant caches -> set header + storage ->
  flip store state -> start timer -> `goto('/')`. Ending: stop timer -> clear header + storage ->
  await purge -> flip state -> toast -> `goto('/')`. Getting this wrong shows the previous scope's
  data under the new banner. `+layout.svelte` wraps `<AppContent>` in `{#key $supportSession.grantUuid}`
  because a same-route `goto` does not refetch.
- **Never compute expiry from the browser clock.** The countdown applies the skew between the
  server's `server_time` (from `listMyGrants`) and `Date.now()` at fetch time.
- **Surface gating** uses `supportSessionGate` (`{active, readOnly}`): `active` hides
  upload/search/chat/export/download/copy; `readOnly` also hides every edit control. Hiding is UX
  only; the backend refuses with `support_grant_write_required` / `_action_not_permitted`, which
  `axios.ts` turns into toasts.
- **WebSocket gap.** `support_access_*` events are routed by `stores/websocket.ts` to
  `$lib/supportAccess/wsHandlers.ts` and re-emitted as the window event `support-access-event`.
  They are unit-tested only; the dev proxy in the browser harness could not carry the socket.

## Styling

- `StatusChip` exists because `ui/Badge` uses brand colours as TEXT (amber on pale tint is 1.8:1).
  Use it for every grant status; keep text at AA in both themes.
- Destructive buttons use `.btn.sa-danger` (`styles/support-access.css`), not `.btn-danger`, for the
  same contrast reason.
- Times and ids render inside `<bdi>` so Arabic (RTL) does not reorder them.

## Gotchas

- Components are legacy-syntax Svelte (`export let`, `createEventDispatcher`), so tests cannot use
  `$on`; assert on dispatched DOM effects or on the callback props.
- Any new literal `t()` key must exist in all 12 locales; build dynamic labels through the
  literal-key Records in `$lib/i18n/keyMaps.ts` (a guard test forbids dynamically built keys).
- Any new module-level state must be registered in (or exempted from) `clearUserState`, or
  `clearUserState.completeness.test.ts` fails.
