# src/lib/types — hand-written mirrors of backend response shapes

## Purpose

Nine files of TypeScript interfaces describing what the API actually returns. Import via
`$lib/types/...`. **This is not the home for every type in the app** — put one here only when
it is shared by several unrelated components.

Where the rest live: search types → `$stores/search.ts` · upload queue types →
`$lib/services/uploadService.ts` · per-endpoint request/response types → colocated in the
`$lib/api/*.ts` module that calls the endpoint · ambient globals → `src/global.d.ts`, `src/vite-env.d.ts`.

## Key files

- `groups.ts` — the most-imported (19 sites); merges **two** backend schemas,
  `backend/app/schemas/group.py` + `sharing.py`, into one file.
- `speakerCluster.ts` — mirrors `schemas/speaker_cluster.py` but **renames** the types
  (`SpeakerCluster` ← `SpeakerClusterResponse`, `PaginatedResponse<T>` ← `PaginatedClusterResponse`),
  so grepping the Python name won't find the TS side.
- `media.ts` — `MediaFile` is a deliberate **subset** of `schemas/media.py:MediaFile` (no
  `storage_path`, `imohash`, …) and is the **gallery/list** shape. `MediaFileDetail extends
MediaFile` with what only `GET /files/{uuid}` returns (`transcript_segments`,
  `grouped_segments`, `asr_model`, `download_url`, `source_url`, `progress`, …) — extend it,
  don't widen `MediaFile`, or list components start depending on fields the list endpoint
  never sends. Also holds `GroupedTranscriptSegment`, the backend-computed overlap grouping
  the frontend must render rather than recompute, and `GroupedSegmentView`, its camelCase
  client-side projection shared by `TranscriptDisplay` and `TranscriptSegmentList`.
- `collection.ts` / `tag.ts` / `comment.ts` — added in #284 A3.6. Before that, `Collection`
  had three competing local declarations, `Tag` one unexported one, and comments had none, so
  every call site fell back to `any[]`. These are now the single home for each.
- `speaker.ts` — `Speaker` + `Segment`; `overlap_index`/`overlap_count` are the only fields marked
  client-computed. `Segment.redactions` is `unknown[]` on purpose — every read site only tests
  whether it is non-empty.
- `summary.ts` — `SummaryData` is intentionally open-ended (`[key: string]: any`) because custom AI
  prompts return arbitrary JSON; the BLUF fields are all optional hints.
- `audioExtraction.ts` — the one file with **no backend counterpart**: browser FFmpeg.wasm types, plus
  the runtime const `DEFAULT_EXTRACTION_CONFIG` (mp3/64 kbps/16 kHz/mono, 2 GB cap).

## Conventions / patterns

- Field names stay **snake_case** — these mirror the JSON wire format verbatim. Don't camelCase them.
  (`audioExtraction.ts` is camelCase precisely because it is client-only.)
- UUIDs are `string`; datetimes are ISO `string`.
- Prefer widening these with the backend's pre-formatted display fields (`formatted_duration`,
  `display_status`, `status_badge_class`, `resolved_speaker_name`) over adding client-side computation.

## Gotchas

- **These are hand-maintained. There is no codegen and no OpenAPI type generation — nothing detects
  drift.** Change a Pydantic schema and you must edit the TS by hand; `svelte-check` passes happily on
  a stale shape because the API is typed `any` at the axios boundary.
- **`MediaFileStatus` is the one mirror with a drift guard.** `media.status.test.ts` parses
  `backend/app/core/enums.py:FileStatus` and asserts the union matches value-for-value, so adding a
  backend status fails that test until the TS is updated. Copy the pattern for other high-traffic
  mirrors. (It exists because the union sat three values behind — `queued`, `downloading`,
  `quarantined` — until #301.)
- **For speaker clusters the Pydantic schema is not the contract.**
  `api/endpoints/speaker_clusters.py` declares `response_model=dict[str, Any]` and the real payload is
  a hand-built dict in `services/speaker_clustering_service.py` — which is why `speakerCluster.ts`
  carries `min_similarity`, `separation_score`, `promoted_to_profile_avatar_url`, `labeled_count`,
  and `unlabeled_count` that `schemas/speaker_cluster.py` does not. Verify against the service.
- `GroupedTranscriptSegment.segments: any[]` and `SummaryData`'s index signature are intentional escape
  hatches — don't narrow them without checking what the backend actually emits.
