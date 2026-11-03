# src/lib/mediaSearch — logic behind the media search page

## Purpose

Request-shaping and preview helpers for `routes/search/+page.svelte` and `components/search/`.
**This is the media (server-side OpenSearch) search; the Settings search box is `$lib/search`.**

## Key files

- `searchParams.ts` — turns the `$stores/search` state into request params (sort, mode, sources,
  speakers, tags, collection, language, file types).
- `previewMedia.ts` — `openPreviewMedia`: fetches a stream URL for a result's clip and re-signs it
  using the URL's real expiry, so long playback does not 401.
- `viewport.ts` — `watchPanelViewport`: the two widths the knowledge panel needs.

## Conventions / patterns

- Never rank, filter or paginate results client-side; the server decides (hybrid RRF).
