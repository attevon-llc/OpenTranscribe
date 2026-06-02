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
  `groups.ts`, `sharing.ts`, `admin.ts`, `adminSettings.ts`, `authConfig.ts`, `transcripts.ts`.

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
