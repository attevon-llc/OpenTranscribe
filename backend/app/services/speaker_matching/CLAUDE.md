# services/speaker_matching/ — implementation of `SpeakerMatchingService`

`speaker_matching_service.SpeakerMatchingService` is the one public entry. It keeps `__init__`,
the privacy/tenant gate (`_scope_gate`, `_voiceprints_allowed`, `_media_file_org_id`) and
`get_confidence_level`, and binds every other method from here as a class attribute. Each
module holds plain functions taking the service as `self`; callers and `patch.object` on the
class are unchanged.

| Module | Concern |
|---|---|
| `thresholds.py` | `ConfidenceLevel`, `MATCHING_ORIGIN_TASK` |
| `match_queries.py` | voiceprint -> known speakers / profiles / unlabeled speakers (read-only) |
| `profiles.py` | profile create/assign, reference-embedding refresh, collections, occurrences |
| `processing.py` | per-file entry points (`process_speaker_segments`, `..._native`) |
| `match_guards.py` | user corrections that block a match; the origin record a match leaves |
| `match_handling.py` | applying a found / not-found match (voiceprint store, profile centroid blend) |
| `propagation.py` | spreading a profile assignment to similar speakers |
| `match_storage.py` | speaker-to-speaker match pairs: kNN query, stored rows, read-back |

Mock targets: `add_speaker_embedding` is imported into `match_handling`; `find_matching_speaker`
into `match_queries` AND `match_storage` (patch both when a flow crosses them);
`embedding_mode_service.voiceprint_model_id` is called through its module so one patch covers
all three callers. Tenant/privacy gating is verbatim: every public entry still goes through
`_scope_gate` / `_voiceprints_allowed`; OpenSearch scores convert via `app.utils.cosine_space`.
