# src/lib/types — hand-written mirrors of backend response shapes

## Purpose

Six files of TypeScript interfaces describing what the API actually returns. Import via
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
- `media.ts` — a deliberate **subset** of `schemas/media.py:MediaFile` (no `storage_path`,
  `download_url`, `imohash`, `asr_provider`, …). Also holds `GroupedTranscriptSegment`, the
  backend-computed overlap grouping the frontend must render rather than recompute.
- `speaker.ts` — `Speaker` + `Segment`; `overlap_index`/`overlap_count` are the only fields marked
  client-computed.
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
- Confirmed drift today: `MediaFile['status']` omits `queued`, `downloading`, and `quarantined`, all of
  which exist in `backend/app/core/enums.py:FileStatus`.
- **For speaker clusters the Pydantic schema is not the contract.**
  `api/endpoints/speaker_clusters.py` declares `response_model=dict[str, Any]` and the real payload is
  a hand-built dict in `services/speaker_clustering_service.py` — which is why `speakerCluster.ts`
  carries `min_similarity`, `separation_score`, `promoted_to_profile_avatar_url`, `labeled_count`,
  and `unlabeled_count` that `schemas/speaker_cluster.py` does not. Verify against the service.
- `GroupedTranscriptSegment.segments: any[]` and `SummaryData`'s index signature are intentional escape
  hatches — don't narrow them without checking what the backend actually emits.
