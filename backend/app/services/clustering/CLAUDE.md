# app/services/clustering — speaker clustering, one module per concern

`SpeakerClusteringService` (`../speaker_clustering_service.py`) is the one public entry. It holds the
state (`db`, the rename tracker, the pending `SpeakerImpact`, the privacy view), the tenant/privacy helpers
(`_scoped_cluster_query`, `_voiceprint_off_scopes`, `_scope_not_in`, `_join`, `_settle_assignments`) and the
public methods **as class attributes bound to the functions here**
(`merge_clusters = admin_ops.merge_clusters`). Every function takes the service as `self`, so a call
inside a module goes through the class (`self._update_cluster_centroid(...)`) and a test that patches
`SpeakerClusteringService.<method>` still intercepts it.

| Module | Concern |
|---|---|
| `assignment` | upload-time placement (`find_or_create_cluster`, `cluster_speakers_for_file`) and membership writes |
| `recluster` | `batch_recluster` orchestrator, clear, separation scores, status |
| `recluster_profiles` | Phase 1 (group by profile) and Phase 2.5 (match singletons to profile clusters) |
| `similarity` | Phase 2: unlabeled embeddings, per-tenant partitions, cosine matrix, complete-linkage AHC |
| `recluster_groups` | post-AHC rules (cannot-link and gender eviction), cluster records, validation |
| `admin_ops` / `cluster_identity` | merge, split / promote to a profile, unassign |
| `verification` / `queries` | the Inbox and batch verify / cluster list and detail |
| `gender`, `centroids`, `thresholds` | gender reads, centroid recompute, shared thresholds |

Patch targets: a method -> `SpeakerClusteringService`; a module-level name (e.g.
`canonical_speaker_label_for_row`) -> the module here that calls it. OpenSearch functions are imported
inside the functions that use them, so patch them on `app.services.opensearch_service`.
Behaviour of `batch_recluster` is pinned by `tests/unit/test_batch_recluster_characterization.py`.
