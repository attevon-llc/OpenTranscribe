# services/search/indexing — per-plane write paths of `TranscriptIndexingService`

`TranscriptIndexingService` (in `../indexing_service.py`) is composed from mixins here:
`chunk_plane` (delete / erasure count / stale-tail prune), `digest_plane`, `summary_plane`,
over `bulk` (bulk write + transient retry, `_suspended_refresh_for_large_index`) and `orphans`
(the realtime `mget` orphan walk shared by the three prunes, issue #435).

⚠️ Each module reads its **own** module-level `opensearch_client`. A test that fakes the cluster
for the whole write path must bind the fake in all of them:
`tests/fixtures/indexing_client.py::bind_indexing_client`. Patching one module leaves the rest on the
real (or absent) client. Likewise `chunk_plane` holds its own `chunk_plane_query` /
`_invalidate_chat_retrieval_cache` bindings, so patch them there, not on `plane_queries`.
