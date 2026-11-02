# src/lib/utils — shared presentational helpers

## Purpose

Pure, framework-light TypeScript helpers used across components. Import via the
`$lib/utils/...` alias. Keep these **presentational only** — business logic, aggregation,
and domain formatting belong in the backend (thin-frontend rule).

## Key files

- `formatting.ts` — **the single home for display formatting** (dates, sizes, durations, relative time, subtitle timestamps). See the rule below.
- `apiError.ts` — standard error handling: `getErrorMessage`, `handleApiError`, `withAsync`.
- `clipboard.ts` — `copyToClipboard(text, onSuccess?, onError?)` with non-secure-context fallback.
- `debounce.ts` — `createDebouncedHandler` (used by search inputs).
- `backoff.ts` — `reconnectDelayMs(attempt)`: exponential backoff **with equal jitter**, capped
  at 30 s. The retry-delay source for WebSocket reconnects (`$stores/websocket`) AND upload
  retries (`$lib/services/uploadService`, `$lib/services/multipartUploader`, H4a) — so a backend
  restart doesn't bring every client back in lockstep. Pass a deterministic `random` in tests.
- `sanitizeHtml.ts` — DOMPurify allowlist wrappers (`sanitizeHighlightHtml`, `sanitizeToPlainText`); every `{@html}` must go through this.
- `speakerColors.ts` — deterministic speaker color assignment (`getSpeakerColor*`).
- `mediaType.ts` — `playableSourceType(contentType)` is the only `<source type>` hint a
  player may render. It returns the canonical type, or `undefined` to omit the attribute,
  because a hint the browser rejects (e.g. `audio/vnd.wave`) stops playback before any bytes
  are fetched (issue #1044). This is the twin of backend `app/utils/media_types.py`.
  Pass it the type `/stream-url` reports (`getCachedUrlInfo(...).contentType`), not the
  upload's: a file no browser decodes is served as an AAC/M4A playback rendition.
- `mediaFormats.ts` — what the player does with each accepted format (plays / converted /
  audio-only preview), plus the extension→MIME fallback map. The upload panel advertises
  these groups, so they must follow backend `services/playback_rendition.py`'s measured
  tables. Moving a format into "playable" without measuring it in both browsers advertises
  playback the user won't get.
- `searchHighlight.ts`, `metadataMapper.ts`, `scrollbarCalculations.ts`, `url.ts`, `ids.ts`.

## Conventions / patterns

- **Display formatting lives ONLY in `formatting.ts`.** A convention test
  (`components/formatters.convention.test.ts`) fails on a same-named copy, and on an inline
  `new Date(x).toLocale*String()`, anywhere else. Use:
  - `formatDate(s, locale?)` / `formatDateTime(value, fallback, locale?)` → app-locale dates
    (never the browser's). Pass `$locale` from a component so a language switch re-renders.
  - `formatAuditTimestamp(s, locale?, { withYear })` → one-call, Latin-digit, bidi-safe
    audit-log timestamp for an LTR cell.
  - `formatFileSize(bytes)`, `formatRelativeTime(date, now, t)`.
  - `formatDuration(s, { padHours? })` → padded `MM:SS` / `HH:MM:SS` (`padHours` keeps a live
    timer one width); `formatClock(s)` → unpadded (`1:05`, `1:01:01`);
    `formatCompactDuration(s)` → `2m 5s`; `formatEta(s)` → compact or `''` when none;
    `formatElapsed(s)` → `1h 0m 5s`.
  - `formatTimeWithMillis(s)` → live player time; `formatSrtTimestamp(s)` /
    `formatVttTimestamp(s)` → subtitle cues (always full `HH`).
  - `getInitials(name, email)` for avatars.
    Time helpers clamp NaN/negative to zero. Behavior is locked by `formatting.test.ts`.
- A component that genuinely needs a different shape names its function for what it does
  (`describeMinutes`, `relativeFromNow`, `formatModelBytes`) instead of reusing a shared name.
- `poller.ts` — `createPoller(callback, { intervalMs })`: a repeating async task that never
  overlaps itself, pauses while the tab is hidden and catches up on resume. Use it instead of a
  bare `setInterval`; `start()` does not run the callback, so do the first load yourself.
- The `Translate` function type (`t` store value) lives in `$lib/i18n`.
- In `.ts` files use `get(store)` from `svelte/store`; `$store` syntax is `.svelte`-only.

## How it connects

- `apiError.ts` composes with `$lib/axios` (`isRequestCancelled`) and `$stores/toast`.
- Backend already sends pre-formatted display fields (`formatted_duration`, `display_status`,
  `resolved_speaker_name`, …) — prefer rendering those over recomputing here.

## Gotchas

- New pure helpers should ship with a colocated `*.test.ts` (Vitest). Run `npm run test`.
