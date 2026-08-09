# src/lib/api — typed API clients

## Purpose

Thin, typed wrappers around backend REST endpoints. One module per domain. Import via
`$lib/api/...`. These are the ONLY place components should reach the backend — components
call these functions, not `axios` directly.

## Key modules

- `mediaUrl.ts` — presigned media stream URLs + caching/refresh (`getMediaStreamUrl`, `createUrlRefresher`).
- `speakerClusters.ts` — clusters/profiles (server-paginated: `listClusters(page, perPage, …)`).
- `transcriptionSettings.ts`, `asrSettings.ts`, `llmSettings.ts`, `prompts.ts`, `userSettings.ts`,
  `redactionSettings.ts`, `downloadSettings.ts`, `audioExtractionSettings.ts`,
  `organizationContext.ts`, `speakerAttributeSettings.ts`, `mediaSourcesApi.ts`, `suggestions.ts`,
  `groups.ts`, `sharing.ts`, `admin.ts`, `adminSettings.ts`, `authConfig.ts`, `invitations.ts`,
  `transcripts.ts`, `groupMappings.ts`, `userApprovals.ts`. `authConfig.ts` covers
  `/admin/auth-config/*` including the auth-mail designation and the config audit log;
  `invitations.ts` covers `/auth/invitations/*` (create/list/revoke are admin, lookup/accept are
  public); `groupMappings.ts` covers `/admin/group-mappings/*` (super_admin — note `GrantableRole`
  is `user|admin` only, never `super_admin`); `userApprovals.ts` covers `/admin/user-approvals/*`
  (admin) and exports `isAlreadyDecided`, the 409 "somebody else decided this first" test that
  callers must surface rather than swallow.

## Conventions / patterns

- Use the shared `axiosInstance` from `$lib/axios` (handles CSRF + 401 refresh + cancellation).
  Never create a separate axios instance or hardcode the API base URL (it comes from
  `$lib/utils/url` / `VITE_API_BASE_URL`).
- Error handling: surface backend errors via `$lib/utils/apiError` (`handleApiError`/`withAsync`)
  in callers; ignore cancelled requests (`isRequestCancelled`).
- **Push filtering/sorting/pagination to the server** as query params — don't fetch-all-then-filter
  in the client. Several modules already do this (clusters, search, gallery).
- Mirror backend Pydantic schemas in the TS types; don't reverse-engineer shapes.

## Gotchas

- Auth/token handling is centralized in `$lib/axios` + `$stores/auth` — don't duplicate it here.
