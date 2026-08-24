# src/lib/utils — shared presentational helpers

## Purpose

Pure, framework-light TypeScript helpers used across components. Import via the
`$lib/utils/...` alias. Keep these **presentational only** — business logic, aggregation,
and domain formatting belong in the backend (thin-frontend rule).

## Key files

- `formatting.ts` — **the single home for time/number/name formatting.** See the rule below.
- `apiError.ts` — standard error handling: `getErrorMessage`, `handleApiError`, `withAsync`.
- `clipboard.ts` — `copyToClipboard(text, onSuccess?, onError?)` with non-secure-context fallback.
- `debounce.ts` — `createDebouncedHandler` (used by search inputs).
- `backoff.ts` — `reconnectDelayMs(attempt)`: exponential backoff **with equal jitter**, capped
  at 30 s. The retry-delay source for WebSocket reconnects (`$stores/websocket`) AND upload
  retries (`$lib/services/uploadService`, `$lib/services/multipartUploader`, H4a) — so a backend
  restart doesn't bring every client back in lockstep. Pass a deterministic `random` in tests.
- `sanitizeHtml.ts` — DOMPurify allowlist wrappers (`sanitizeHighlightHtml`, `sanitizeToPlainText`); every `{@html}` must go through this.
- `speakerColors.ts` — deterministic speaker color assignment (`getSpeakerColor*`).
- `searchHighlight.ts`, `metadataMapper.ts`, `scrollbarCalculations.ts`, `url.ts`, `ids.ts`.

## Conventions / patterns

- **Time formatting lives ONLY in `formatting.ts`.** Do not write a local `formatTime`/
  `formatTimestamp`/`formatDuration` in a component. Use:
  - `formatDuration(s)` → padded `MM:SS` / `HH:MM:SS` (e.g. `01:05`).
  - `formatClock(s)` → unpadded, hour-aware (`1:05`, `1:01:01`).
  - `formatTimeWithMillis(s)` → live player time `HH:MM:SS.mmm`.
  - `formatSrtTimestamp(s)` / `formatVttTimestamp(s)` → subtitle cues (always full `HH`).
  - `getInitials(name, email)` for avatars.
    All clamp NaN/negative to zero. Behavior is locked by `formatting.test.ts`.
- **Do NOT migrate** to the shared formatters: relative "X ago" labels, compact `Xh Ym`
  durations, ETA formatters returning `undefined`, or unpadded formatters that intentionally
  show minutes ≥ 60 without hour rollover — those differ from the shared ones.
- In `.ts` files use `get(store)` from `svelte/store`; `$store` syntax is `.svelte`-only.

## How it connects

- `apiError.ts` composes with `$lib/axios` (`isRequestCancelled`) and `$stores/toast`.
- Backend already sends pre-formatted display fields (`formatted_duration`, `display_status`,
  `resolved_speaker_name`, …) — prefer rendering those over recomputing here.

## Gotchas

- New pure helpers should ship with a colocated `*.test.ts` (Vitest). Run `npm run test`.
