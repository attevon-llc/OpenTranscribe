# Speaker Pre-Clustering & Global Profile Management: Research & Architecture

> **Status: IMPLEMENTED in v0.4.0**
>
> The architecture described in this document has been fully implemented. Key implemented items:
> - Global `/speakers` page with Clusters, Profiles, and Inbox tabs
> - Audio clip playback (play/pause) for rapid speaker identification
> - Cluster merge, split, and promote-to-profile operations
> - GPU-accelerated speaker embedding extraction (Celery embedding worker)
> - Batch labeling: name a cluster once, propagates to all instances
> - Profile avatars (added in migration `v270_add_profile_avatar.py`)
> - Gender-informed cluster validation (added in migration `v300_add_gender_confirmed.py`)
> - Cluster quality metrics (added in migration `v260_add_cluster_quality_metrics.py`)
> - Auto-labeling (added in migration `v230_add_auto_labeling.py`)
> - Full Alembic migration chain: v220 through v320 covers all schema changes
>
> This document is retained as a research reference and architectural record.

> Research conducted February 2026 for OpenTranscribe speaker management at scale (1,000+ videos).

## Table of Contents

1. [Problem Statement](#problem-statement)
2. [Current System Architecture](#current-system-architecture)
3. [Industry Landscape](#industry-landscape)
   - [Commercial Products](#commercial-products)
   - [Enterprise Approaches](#enterprise-approaches)
   - [Open Source Projects](#open-source-projects-github)
4. [Clustering Algorithms](#clustering-algorithms-for-speaker-embeddings)
5. [OpenSearch Capabilities](#opensearch-capabilities-for-vector-clustering)
6. [Recommended Architecture](#recommended-architecture)
7. [Edge Cases & Mitigations](#edge-cases--mitigations)
8. [Performance Analysis](#performance-analysis-at-scale)
9. [Implementation Roadmap](#implementation-roadmap)
10. [References](#references)

---

## Problem Statement

OpenTranscribe currently handles speaker identification on a **per-file basis**: upload a video, diarize speakers (SPEAKER_00, SPEAKER_01, etc.), extract 256-dim WeSpeaker embeddings, match against existing profiles via OpenSearch kNN, and present suggestions in the transcript view. Users must navigate file-by-file to label speakers and verify matches.

**At 1,000+ videos this workflow is impractical.** Users need:

- **Pre-clustering**: Automatically group unnamed speakers across all files into clusters representing likely-unique individuals
- **Global speaker management**: A dedicated page to view, label, merge, and split speaker clusters without opening individual transcripts
- **Audio clip playback**: Quick voice samples for rapid speaker identification
- **Batch labeling**: Name a cluster once and propagate to all instances across all files
- **Speaker inbox**: A prioritized queue for processing unverified speakers efficiently

---

## Current System Architecture

### Embedding Pipeline

| Component | Technology | Details |
|-----------|-----------|---------|
| Diarization | PyAnnote speaker-diarization-3.1 | Produces SPEAKER_00, SPEAKER_01, etc. with overlap detection |
| Embeddings (v4) | WeSpeaker ResNet34-LM | 256-dim centroids, L2-normalized, extracted during diarization |
| Embeddings (v3) | pyannote/embedding | 512-dim, separate model load (legacy mode) |
| Vector storage | OpenSearch 3.4.0 (HNSW) | Lucene engine, cosinesimil, ef_construction=128, m=24 |
| Profile consolidation | L2-normalized mean | Average all assigned speaker embeddings per profile |

### Matching Pipeline

```
New Speaker Embedding
    |
    v
Tier 1: Match against SpeakerProfile embeddings (document_type="profile")
    |-- HIGH confidence (>=0.75): Auto-accept, assign to profile, propagate
    |-- MEDIUM confidence (>=0.50): Store as suggestion for user review
    |
    v
Tier 2: Match against individual Speaker embeddings (cross-file)
    |-- Returns voice match suggestions
    |
    v
Tier 3: No match found
    |-- Store embedding in OpenSearch for future matching
```

### Key Services

| Service | File | Purpose |
|---------|------|---------|
| SpeakerEmbeddingService | `backend/app/services/speaker_embedding_service.py` | PyAnnote embedding extraction, warm model cache |
| SpeakerMatchingService | `backend/app/services/speaker_matching_service.py` | Three-tier matching, profile propagation |
| ProfileEmbeddingService | `backend/app/services/profile_embedding_service.py` | Profile-level embedding consolidation |
| SmartSpeakerSuggestionService | `backend/app/services/smart_speaker_suggestion_service.py` | LLM + Profile + Voice suggestion consolidation |
| SpeakerStatusService | `backend/app/services/speaker_status_service.py` | Computed status (verified/suggested/unverified) |
| OpenSearchService | `backend/app/services/opensearch_service.py` | kNN search, bulk operations, index management |

### Data Model

```
SpeakerProfile (global identity)
    |-- name, description, embedding_count, last_embedding_update
    |-- 1:N relationship to Speaker instances
    |
Speaker (per-file instance)
    |-- name (SPEAKER_01), display_name (John Smith)
    |-- suggested_name, suggestion_source, confidence
    |-- profile_id (FK to SpeakerProfile)
    |-- verified (boolean)
    |
SpeakerMatch (cross-reference)
    |-- Links similar speakers across files with confidence scores
    |
SpeakerCollection (organizational grouping)
    |-- User-defined groupings of speaker profiles
```

### What's Missing for 1000-Video Scale

1. **No pre-clustering** -- Unnamed speakers across files aren't grouped into clusters
2. **No global management page** -- Must navigate file-by-file to manage speakers
3. **No audio clip playback** -- Can't quickly listen to a speaker sample to identify them
4. **No batch labeling** -- Must verify speakers one-at-a-time, one-file-at-a-time
5. **No speaker inbox** -- No prioritized queue for processing unverified speakers
6. **No periodic re-clustering** -- Matching only runs at transcription time
7. **No cluster coherence monitoring** -- No detection of false-positive merges
8. **No profile split capability** -- Once merged, no way to undo

---

## Industry Landscape

### Commercial Products

#### Trint -- Team-Shared Speaker Library

Trint offers the most advanced cross-file speaker management among transcription platforms. Their [Auto Speaker Recognition](https://trint.com/blog/auto-speaker-recognition) feature provides:

- **Shared voiceprint library**: Once one team member enrolls a speaker, all team members benefit instantly
- **Automatic tagging**: The AI auto-labels speakers in future transcripts using the persistent library
- **Human-in-the-loop**: When uncertain, suggests rather than auto-applies
- **GDPR compliance**: Voiceprints treated as biometric data with appropriate data handling

**Relevance**: Validates OpenTranscribe's `SpeakerProfile` architecture. The team-sharing model maps to `SpeakerCollection`.

#### Otter.ai -- Progressive Voice Learning

Otter.ai's [Speaker Identification](https://help.otter.ai/hc/en-us/articles/21665587209367-Speaker-Identification-Overview) system:

- **Account-level profiles**: Learns to recognize individual voices over time
- **Workspace sharing**: Speaker profiles shared across team channels
- **Rematch feature**: Retroactively re-identifies speakers in old recordings after new profiles are created
- **Minimal training**: Tag a few paragraphs per speaker, ML learns voice patterns

**Relevance**: The "Rematch" feature is a key missing capability -- OpenTranscribe should allow re-running speaker matching on old files after profiles improve.

#### Descript -- Integrated Voice Learning

Descript [learns voices over time](https://help.descript.com/hc/en-us/articles/10249423506061-Automatic-Speaker-Detection), automatically applying correct names to recurring speakers without manual labeling. Speaker recognition is integrated into their broader audio/video editing workflow.

#### Microsoft Teams -- Biometric Enrollment

Microsoft Teams has the most developed enterprise [speaker identification system](https://learn.microsoft.com/en-us/microsoftteams/rooms/voice-and-face-recognition):

- **Voice & face profiles**: Users create voice profiles by reading a text sample
- **Cross-meeting persistence**: Once enrolled, automatically identified in all future meetings
- **Copilot integration**: Speaker identity is essential input for meeting summaries
- **Limitations**: Max 20 enrolled profiles per meeting, recommended max 10 in-person attendees
- **Privacy**: Voice/face data encrypted, auto-deleted after 1 year of non-use

#### Picovoice Eagle -- On-Device Speaker Recognition

[Eagle](https://github.com/Picovoice/eagle) provides an on-device two-phase architecture:

- **Enrollment**: Produces binary `Profile` objects that can be persisted
- **Recognition**: Compares incoming audio against enrolled profiles in real-time
- **Quality feedback**: Enrollment percentage concept (keep enrolling until 100%)
- **Claims to outperform** SpeechBrain, pyannote, and WeSpeaker on detection accuracy

**Relevance**: The enrollment quality percentage is an excellent UX pattern for OpenTranscribe's profile quality indicators.

#### AssemblyAI -- Per-File Only

AssemblyAI explicitly states they [do not offer cross-file speaker identification](https://www.assemblyai.com/docs/faq/do-you-offer-cross-file-speaker-identification). For cross-file identification, they recommend building a custom solution using [audio embeddings with Pinecone and NVIDIA TitaNet](https://www.assemblyai.com/docs/speech-understanding/speaker-identification).

### Summary: Commercial Product Patterns

| Pattern | Products | Adopt? |
|---------|----------|--------|
| Persistent speaker library | Trint, Otter, Descript, Teams | Yes -- already have SpeakerProfile |
| Team/workspace sharing | Trint, Otter | Yes -- already have SpeakerCollection |
| Rematch old recordings | Otter | **Yes -- new feature needed** |
| Enrollment quality indicator | Picovoice Eagle | **Yes -- new feature needed** |
| Voice profile enrollment | Teams | Consider for future |
| Cascade rename | All | Already implemented |
| Audio clip save/share | Rev | **Yes -- new feature needed** |

---

### Enterprise Approaches

#### BBC World Service Archive (Raimond et al., 2014)

One of the largest public speaker identification experiments:

- **Scale**: 70,000 English-language radio programmes (~15TB, 45 years of audio)
- **Result**: ~1 million speaker models
- **Technology**: Supervector-based speaker models + distributed Locality Sensitive Hashing (LSH) index
- **Crowdsourcing**: Users name speakers, identities propagate to other programmes
- **Finding**: 10 users did 70% of the labeling work (power law distribution)

**Lessons for OpenTranscribe**:
- LSH (the predecessor to HNSW) was sufficient for 1M speaker models -- our HNSW approach is more than adequate
- Crowdsourced corrections improved algorithm quality over time -- same principle applies to user verification
- UI should be optimized for power users who will do most of the labeling

#### Pindrop -- Enterprise Voice Biometrics

- **Scale**: 5B+ calls processed
- **Approach**: Multi-factor voice biometrics (voice + device + behavior + network)
- **Finding**: Fraud rates rising due to voice cloning (350+ tools available)
- **Lesson**: Speaker profile trust should be based on multiple verification signals, not just embedding similarity

#### NVIDIA NeMo -- Speaker Recognition Pipeline

- **Models**: TitaNet and SpeakerNet for embedding extraction
- **Verification threshold**: Cosine similarity of 0.7 (reference for our thresholds)
- **Streaming Sortformer**: Arrival-Order Speaker Cache (AOSC) for persistent cross-session identity
- **Batch inference API**: Efficient extraction from many files in parallel

#### Google Recorder -- On-Device Solution

Published at ICASSP 2022 as "Turn-to-Diarize":
- **Architecture**: Speaker turn detection model + speaker encoder for d-vector extraction + multi-stage clustering
- **Processing**: Entirely on-device
- **Privacy**: Voice models treated as biometric data, deleted after processing

---

### Open Source Projects (GitHub)

#### Resemblyzer (resemble-ai/Resemblyzer)
- **Stars**: 2K+ | [GitHub](https://github.com/resemble-ai/Resemblyzer)
- **What**: Python package deriving 256-dim voice embeddings from audio files
- **Speed**: ~1000x real-time on GTX 1080
- **Relevance**: Validates our 256-dim embedding + cosine similarity approach
- **Pattern**: A [production implementation](https://codingwithcody.com/2025/04/02/containerized-voice-identification/) pairs Resemblyzer with Qdrant vector DB in a FastAPI container -- architecturally identical to our OpenSearch approach

#### WeSpeaker (wenet-e2e/wespeaker)
- **Stars**: 1K+ | [GitHub](https://github.com/wenet-e2e/wespeaker)
- **What**: Production-oriented speaker verification toolkit (already used by OpenTranscribe via PyAnnote v4)
- **Key finding**: August 2024 update added **UMAP + HDBSCAN clustering recipe** for VoxConverse dataset
- **Relevance**: State-of-the-art approach for clustering speaker embeddings, directly applicable to cross-file clustering

#### SpectralCluster (Google) (wq2012/SpectralCluster)
- **Stars**: 400+ | [GitHub](https://github.com/wq2012/SpectralCluster)
- **What**: Python re-implementation of Google's constrained spectral clustering for speaker diarization
- **Features**: Auto-tuning of K, cosine distance, CropDiagonal refinement
- **Relevance**: Alternative to DBSCAN/HDBSCAN for discovering number of unique speakers across a library

#### 3D-Speaker (ModelScope)
- [GitHub](https://github.com/modelscope/3D-Speaker)
- **What**: Comprehensive speaker verification/recognition/diarization toolkit (ICASSP 2025)
- **Scale**: Models trained on 200K-speaker datasets
- **Relevance**: Validates that embedding models handle large speaker populations

#### Picovoice Eagle
- [GitHub](https://github.com/Picovoice/eagle)
- **What**: On-device speaker recognition with enrollment/recognition architecture
- **Relevance**: Enrollment quality feedback UX pattern; binary profile persistence model

#### SpeechBrain
- [GitHub](https://github.com/speechbrain/speechbrain)
- **What**: PyTorch toolkit with ECAPA-TDNN for speaker verification
- **Relevance**: Competitive alternative to WeSpeaker if embedding quality needs improvement; `verify_batch()` function useful for merge validation

#### DBSCAN Cross-File Pattern
- [Medium article](https://medium.com/@sapkotabinit2002/speaker-identification-and-clustering-using-pyannote-dbscan-and-cosine-similarity-dfa08b5b2a24)
- **What**: End-to-end pipeline: PyAnnote diarization -> embedding extraction -> cosine similarity matrix -> DBSCAN clustering across files
- **Relevance**: Most directly applicable implementation for OpenTranscribe's cross-file speaker identification

#### Online Speaker Clustering
- [GitHub](https://github.com/sholokhovalexey/online-speaker-clustering)
- **What**: Block-online k-means with look-ahead for streaming speaker data
- **Relevance**: Pattern for incremental cluster assignment as new files arrive

---

## Clustering Algorithms for Speaker Embeddings

### Algorithm Comparison

| Algorithm | K Required? | Handles Outliers? | Incremental? | Complexity | Best For |
|-----------|------------|-------------------|-------------|------------|----------|
| **HDBSCAN** | No | Yes (noise points) | No (batch) | O(n log n) | Batch re-clustering, variable density |
| **Connected Components** (Union-Find) | No | No | Yes | O(V + E) | Real-time assignment from similarity graph |
| **K-Means** | Yes | No | Limited | O(nKI) | Server-side in OpenSearch ML Commons |
| **DBSCAN** | No (but needs epsilon) | Yes | No (batch) | O(n log n) | Cross-file clustering with fixed threshold |
| **Agglomerative** | Threshold-based | No | No | O(n^2 log n) | Small-medium datasets, dendrogram analysis |
| **Spectral** | Yes (or auto-tune) | No | No | O(n^3) | Within-file diarization (already used by PyAnnote) |

### Recommended: Hybrid Approach

**Real-time (per-file)**: Connected Components via Union-Find
- When a new file is transcribed, kNN search against existing cluster centroids in OpenSearch
- If match >= threshold (0.65), assign to existing cluster and update centroid
- If no match, create new singleton cluster
- O(1) per speaker via HNSW, no batch processing needed

**Periodic batch (on-demand)**: UMAP + HDBSCAN (following WeSpeaker recipe)
- Scroll all unverified speaker embeddings from OpenSearch
- Apply UMAP to reduce 256-dim to ~10-20 dims
- Run HDBSCAN to discover clusters (no K needed, handles noise)
- Map clusters back to SpeakerCluster records
- Useful for discovering clusters missed by incremental assignment

**Optional server-side**: OpenSearch ML Commons k-means
- For coarse cluster discovery when <10K speakers
- `POST /_plugins/_ml/_train_predict/kmeans` with `distance_type: COSINE`
- 10K document limit per training run

### Cosine Similarity Thresholds

Based on industry research and production systems:

| Threshold | Use | Reference |
|-----------|-----|-----------|
| >= 0.90 | Auto-accept same identity (safe for automated merging) | Pindrop, OpenTranscribe internal |
| 0.75 - 0.90 | Strong suggestion, user confirmation recommended | OpenTranscribe SPEAKER_CONFIDENCE_HIGH |
| 0.65 - 0.75 | Cluster formation threshold (group likely-same speakers) | WeSpeaker recipe, DBSCAN literature |
| 0.50 - 0.65 | Weak suggestion, present as option | OpenTranscribe SPEAKER_CONFIDENCE_MEDIUM |
| < 0.50 | Different speakers, do not suggest | Industry standard |

### Incremental Clustering Strategy

```
New File Transcribed
    |
    v
Extract speaker embeddings (already implemented)
    |
    v
For each unnamed speaker:
    |
    +-- Query OpenSearch: kNN against cluster centroids (document_type="cluster")
    |       |
    |       +-- Match >= 0.65: Add to cluster, update centroid (weighted average)
    |       +-- No match: Create new singleton cluster, store centroid
    |
    v
Periodic (daily or manual):
    |
    +-- Fetch all unverified embeddings from OpenSearch
    +-- Run HDBSCAN batch clustering
    +-- Merge/create clusters based on new groupings
    +-- Update OpenSearch cluster centroids
```

---

## OpenSearch Capabilities for Vector Clustering

### What OpenSearch Can Do Natively

#### 1. kNN Vector Search (HNSW) -- Already In Use

```json
{
  "knn": {
    "embedding": {
      "vector": [0.1, 0.2, ...],
      "k": 10,
      "filter": {
        "bool": {
          "filter": [
            {"term": {"user_id": 42}},
            {"term": {"document_type": "cluster"}}
          ]
        }
      }
    }
  }
}
```

Performance: sub-ms latency for 100K+ vectors with HNSW (ef_construction=128, m=24).

#### 2. ML Commons k-means Clustering

```json
POST /_plugins/_ml/_train_predict/kmeans
{
  "parameters": {
    "centroids": 10,
    "iterations": 20,
    "distance_type": "COSINE"
  },
  "input_query": {
    "_source": ["embedding"],
    "size": 10000,
    "query": {
      "bool": {
        "filter": [
          {"term": {"user_id": 42}},
          {"bool": {"must_not": {"term": {"document_type": "profile"}}}}
        ]
      }
    }
  },
  "input_index": ["speakers"]
}
```

**Limitations**: 10K document cap, requires pre-specifying K, no DBSCAN/HDBSCAN.

#### 3. Multi-Search (msearch) for Batch kNN

```json
POST /speakers/_msearch
{"index": "speakers"}
{"size": 10, "query": {"knn": {"embedding": {"vector": [...], "k": 10, "filter": {"term": {"user_id": 42}}}}}}
{"index": "speakers"}
{"size": 10, "query": {"knn": {"embedding": {"vector": [...], "k": 10, "filter": {"term": {"user_id": 42}}}}}}
```

Batch 50+ kNN queries per request for building similarity graphs.

#### 4. RCF Summarize (CURE-based Hierarchical Clustering)

- Auto-determines number of clusters (no K required)
- Uses `max_k` parameter instead of fixed count
- **Limitation**: Only supports L2 distance (not cosine). However, since our embeddings are L2-normalized, L2 distance is monotonically related to cosine distance.

#### 5. Index Warm-Up

```
GET /_plugins/_knn/warmup/speakers
```

Proactively loads HNSW graph into memory for faster first queries.

### What OpenSearch Cannot Do

| Capability | Status | Workaround |
|------------|--------|------------|
| DBSCAN/HDBSCAN clustering | Not available | Pull embeddings to Python, use scikit-learn/hdbscan library |
| Vector aggregations (group by similarity) | Not available | Use kNN + application-level graph analysis |
| HNSW graph traversal | Internal, not exposed | Use kNN queries to approximate graph structure |
| Incremental cluster assignment | Not available (ML Commons is batch-only) | Application-level centroid routing |

### Recommended Hybrid Architecture

```
                    OpenSearch                          Python
                 (vector operations)              (graph analysis)
                        |                               |
Real-time:     kNN search against               Connected-components
(per-file)     cluster centroids                (Union-Find) for
               document_type="cluster"          cluster assignment
                        |                               |
Batch:         msearch (50 kNN queries/req)     HDBSCAN clustering
(periodic)     builds similarity graph          discovers new clusters
                        |                               |
Optional:      ML Commons k-means              Post-process results
(server-side)  (<10K speakers)                 map back to DB records
```

### Index Design

**Keep single `speakers` index** with `document_type` field (already in use):

| document_type | Description | Document ID |
|---------------|-------------|-------------|
| `"speaker"` | Individual speaker embedding | `{speaker_uuid}` |
| `"profile"` | Consolidated profile embedding | `profile_{profile_uuid}` |
| `"cluster"` | Cluster centroid embedding | `cluster_{cluster_uuid}` |

The Lucene engine's smart pre-filtering efficiently handles the `document_type` filter for kNN queries.

### Performance Characteristics

| Operation | Complexity | Latency (1K speakers) | Latency (10K speakers) |
|-----------|------------|----------------------|------------------------|
| Single kNN query | O(log n) | <1ms | <5ms |
| msearch (50 queries) | O(50 * log n) | <10ms | <50ms |
| Full similarity graph (N speakers) | O(N/50 * log n) | ~200ms | ~2s |
| ML Commons k-means | O(nKI) | <5s | <30s |

---

## Recommended Architecture

### Database Schema Changes

**New: `speaker_cluster` table**

| Column | Type | Description |
|--------|------|-------------|
| id | SERIAL PRIMARY KEY | Internal ID |
| uuid | UUID UNIQUE | External identifier |
| user_id | INTEGER FK | Owner |
| label | VARCHAR | User-assigned label (null until named) |
| description | TEXT | Optional notes |
| member_count | INTEGER | Number of speakers in cluster |
| promoted_to_profile_id | INTEGER FK | If promoted to SpeakerProfile |
| representative_speaker_id | INTEGER FK | Best speaker for audio clip |
| created_at | TIMESTAMPTZ | Creation time |
| updated_at | TIMESTAMPTZ | Last update |

**New: `speaker_cluster_member` table**

| Column | Type | Description |
|--------|------|-------------|
| id | SERIAL PRIMARY KEY | Internal ID |
| uuid | UUID UNIQUE | External identifier |
| cluster_id | INTEGER FK | Parent cluster |
| speaker_id | INTEGER FK | Member speaker |
| confidence | FLOAT | Confidence of assignment |
| UNIQUE(cluster_id, speaker_id) | | Prevent duplicates |

**New: `speaker_audio_clip` table**

| Column | Type | Description |
|--------|------|-------------|
| id | SERIAL PRIMARY KEY | Internal ID |
| uuid | UUID UNIQUE | External identifier |
| speaker_id | INTEGER FK | Source speaker |
| media_file_id | INTEGER FK | Source media file |
| storage_path | VARCHAR | MinIO path |
| start_time | FLOAT | Start time in source |
| end_time | FLOAT | End time in source |
| duration | FLOAT | Clip duration |
| is_representative | BOOLEAN | Best clip for this speaker |
| quality_score | FLOAT | Based on segment length, confidence |

**Modified: `speaker` table**
- Add `cluster_id` (FK to speaker_cluster)

**Modified: `speaker_profile` table**
- Add `source_cluster_id` (FK to speaker_cluster)

### Clustering Service

Core service implementing the hybrid approach:

```python
class SpeakerClusteringService:
    """Speaker clustering using OpenSearch kNN + Union-Find."""

    def find_or_create_cluster(speaker_id, user_id, embedding):
        """Real-time: kNN against cluster centroids, assign or create new."""

    def build_clusters_from_matches(user_id):
        """Batch: Connected-components via Union-Find on SpeakerMatch table."""

    def batch_recluster(user_id):
        """Batch: msearch similarity graph -> HDBSCAN -> update clusters."""

    def merge_clusters(source_id, target_id):
        """Merge source cluster into target, recalculate centroid."""

    def split_cluster(cluster_id, speaker_ids):
        """Split specified speakers into new cluster."""

    def promote_cluster_to_profile(cluster_id, name):
        """Convert cluster to SpeakerProfile, propagate to all members."""

    def update_cluster_centroid(cluster_id):
        """Recalculate L2-normalized mean centroid, store in OpenSearch."""
```

### Audio Clip Service

```python
class SpeakerAudioClipService:
    """Extract and manage speaker audio clips for identification."""

    def extract_clip_for_speaker(speaker_id, media_file_id):
        """Select best segment, extract 3-10s clip via ffmpeg, upload to MinIO."""

    def select_best_segment(speaker_id, media_file_id):
        """Choose longest non-overlap segment with highest confidence."""

    def get_representative_clip(speaker_id):
        """Get the best clip across all files for a speaker."""
```

Clip format: WebM/Opus, 48kbps, ~50KB per clip. Stored at `speaker-clips/{user_id}/{speaker_uuid}.webm` in MinIO.

### Frontend: Global Speaker Management Page

Three-tab layout at `/speakers`:

**Clusters tab**: Auto-discovered speaker groups with audio playback, inline naming, merge/split
**Profiles tab**: Named profiles with stats, appearances, quality indicators
**Inbox tab**: Prioritized queue of unverified speakers with keyboard shortcuts (A/R/N/S)

---

## Edge Cases & Mitigations

### Voice Variation Across Recordings

**Problem**: Same speaker sounds different due to microphone, room acoustics, emotional state, or health.

**Research findings** (PMC studies): Background noise at SNR below 30 dB significantly impacts acoustic measurements. Reverberation in large rooms alters voice quality measures.

**Mitigation**:
- Profile embeddings average over multiple samples, improving robustness (already implemented)
- WeSpeaker ResNet34-LM is trained to be robust to channel effects
- HDBSCAN noise handling treats uncertain embeddings as outliers rather than forcing assignment
- Store multiple embeddings per profile (from different recordings) -- not just the centroid

### False Positive Merges

**Problem**: Two different speakers merged into one profile due to similar voices.

**Prevention**:
- Conservative auto-merge threshold (>=0.90 for automated, >=0.75 for suggestion)
- Always require human confirmation for 0.75-0.90 range
- Profile coherence monitoring: flag profiles where constituent embeddings have high internal variance (min pairwise similarity < 0.60)

**Recovery**:
- "Split profile" feature preserves original per-file speaker embeddings in OpenSearch
- Splitting reconstructs separate profiles from the original embeddings

### Cluster Fragmentation

**Problem**: Same speaker split across multiple clusters due to embedding variance.

**Mitigation**:
- Periodic batch re-clustering (HDBSCAN) discovers clusters missed by incremental assignment
- Manual merge UI for combining clusters
- Centroid averaging stabilizes over time as more samples are added
- Profile quality indicator shows users when a profile needs more samples

### Scaling Concerns

**Problem**: kNN search performance as speaker count grows.

**Analysis** (based on AWS guidance and Uber's billion-scale deployment):
- HNSW with Lucene engine handles 100K+ vectors at sub-ms latency
- OpenTranscribe's current params (ef_construction=128, m=24) are well-suited
- For 10K speakers + 500 clusters + 200 profiles, the index has ~10,700 documents -- well within single-shard capacity
- Even at 100K speakers, no architectural changes needed

### Race Conditions

**Problem**: Multiple files transcribed simultaneously, concurrent cluster updates.

**Mitigation**:
- `SELECT FOR UPDATE` on cluster rows during centroid updates
- `UNIQUE(cluster_id, speaker_id)` constraint on membership table prevents duplicates
- Celery tasks processed on CPU queue, serialized per user if needed

### Privacy Considerations

Speaker embeddings are effectively voice biometrics. Key considerations:

- **GDPR/BIPA compliance**: Biometric data has special protections in many jurisdictions
- **Trint's approach**: Voiceprints treated as biometric data under GDPR
- **Microsoft Teams**: Encrypts voice data, auto-deletes after 1 year of non-use
- **Google Recorder**: Processes on-device, deletes voice models after processing

**Recommendations for OpenTranscribe**:
1. Document that speaker embeddings are stored and how they are used
2. Provide "delete all my voice data" option per user
3. Implement configurable data retention policies
4. Position on-premise deployment as a privacy advantage
5. Allow administrators to disable speaker embedding storage entirely

---

## Performance Analysis at Scale

### 1,000 Videos Scenario

Assumptions: 1,000 videos, average 3 speakers per video, ~3,000 total speaker instances, ~200 unique individuals.

| Operation | Performance |
|-----------|-------------|
| OpenSearch kNN (per speaker) | <1ms |
| Cluster assignment (per file, 3 speakers) | <10ms total |
| Full re-clustering (3K speakers) | ~2s (msearch graph) + ~1s (HDBSCAN) |
| Audio clips storage | ~150MB in MinIO (3K clips x 50KB) |
| Union-Find memory | ~24KB for 3K nodes |
| Frontend load (paginated) | <100ms for 20 items per page |

### 10,000 Videos Scenario

Assumptions: 10,000 videos, ~30,000 speaker instances, ~1,000 unique individuals.

| Operation | Performance |
|-----------|-------------|
| OpenSearch kNN (per speaker) | <5ms |
| Full re-clustering (30K speakers) | ~20s (msearch) + ~5s (HDBSCAN) |
| Audio clips storage | ~1.5GB in MinIO |
| OpenSearch index size | ~30MB (30K x 1KB documents) |
| Union-Find memory | ~240KB |

---

## Implementation Roadmap

All phases completed in v0.4.0.

### Phase 1: Database & Core Service — DONE
- Alembic migrations v220-v320 (speaker clusters, auto-labeling, cluster indexes, quality metrics, profile avatars, gender confirmed, cluster constraints, cluster suggested names)
- SQLAlchemy models for `SpeakerCluster` and `SpeakerClusterMember`
- SpeakerClusteringService with Union-Find
- OpenSearch cluster document type support

### Phase 2: Celery Tasks — DONE
- `cluster_speakers_for_file` (post-transcription, embedding worker)
- `recluster_all_speakers` (manual/periodic, utility worker)
- `extract_speaker_audio_clips` (post-transcription)
- Dedicated `celery-embedding-worker` for speaker embedding extraction

### Phase 3: API Endpoints — DONE
- CRUD for clusters
- Promote, merge, split operations
- Audio clip streaming from MinIO
- Unverified speakers inbox
- Batch verify

### Phase 4: Frontend — DONE
- Global `/speakers` page with Clusters, Profiles, Inbox tabs
- AudioClipPlayer component with play/pause
- Cluster/Profile/Inbox cards with stats
- Profile avatar support
- Navbar integration

### Phase 5: Testing & Verification — DONE
- Unit tests for clustering service
- API endpoint tests
- Performance validated at 1K+ video scale

---

## References

### Commercial Products
- [Trint Auto Speaker Recognition](https://trint.com/blog/auto-speaker-recognition)
- [Otter.ai Speaker Identification Overview](https://help.otter.ai/hc/en-us/articles/21665587209367-Speaker-Identification-Overview)
- [Descript Speaker Detection](https://help.descript.com/hc/en-us/articles/10249423506061-Automatic-Speaker-Detection)
- [Microsoft Teams Voice Recognition](https://learn.microsoft.com/en-us/microsoftteams/rooms/voice-recognition)
- [Microsoft Teams Voice and Face Enrollment](https://learn.microsoft.com/en-us/microsoftteams/rooms/voice-and-face-recognition)
- [Microsoft Teams Speaker Recognition with Copilot](https://techcommunity.microsoft.com/blog/microsoftteamsblog/get-the-most-out-of-any-teams-rooms-meeting-with-speaker-recognition-and-copilot/4182595)
- [AssemblyAI Cross-File Speaker Identification FAQ](https://www.assemblyai.com/docs/faq/do-you-offer-cross-file-speaker-identification)
- [Rev Transcription Editor](https://support.rev.com/hc/en-us/articles/29824992702989-Transcription-Editor)

### Enterprise & Research
- [BBC World Service Speaker Identification (ScienceDirect)](https://www.sciencedirect.com/science/article/abs/pii/S1570826814000535)
- [Pindrop Voice Biometrics](https://www.pindrop.com/technologies)
- [NVIDIA NeMo Speaker Recognition](https://docs.nvidia.com/nemo-framework/user-guide/latest/nemotoolkit/asr/speaker_recognition/intro.html)
- [NVIDIA Streaming Sortformer](https://developer.nvidia.com/blog/identify-speakers-in-meetings-calls-and-voice-apps-in-real-time-with-nvidia-streaming-sortformer/)
- [Google Recorder Turn-to-Diarize](https://research.google/blog/who-said-what-recorders-on-device-solution-for-labeling-speakers/)
- [Spotify Spoken Language Identification](https://research.atspotify.com/publications/lightweight-and-efficient-spoken-language-identification-of-long-form-audio/)
- [SpeakerLM End-to-End Diarization (arXiv 2025)](https://arxiv.org/html/2508.06372v1)
- [Top Speaker Diarization Libraries 2025 (AssemblyAI)](https://www.assemblyai.com/blog/top-speaker-diarization-libraries-and-apis)

### Open Source Projects
- [Resemblyzer](https://github.com/resemble-ai/Resemblyzer) -- 256-dim voice embeddings
- [WeSpeaker](https://github.com/wenet-e2e/wespeaker) -- Speaker verification with UMAP+HDBSCAN
- [SpectralCluster (Google)](https://github.com/wq2012/SpectralCluster) -- Spectral clustering for diarization
- [3D-Speaker (ModelScope)](https://github.com/modelscope/3D-Speaker) -- 200K-speaker trained models
- [Picovoice Eagle](https://github.com/Picovoice/eagle) -- On-device speaker recognition
- [SpeechBrain](https://github.com/speechbrain/speechbrain) -- PyTorch speaker verification
- [pyannote-audio](https://github.com/pyannote/pyannote-audio) -- State-of-the-art diarization
- [awesome-diarization](https://github.com/wq2012/awesome-diarization) -- Curated resource list
- [awesome-speaker-embedding](https://github.com/ranchlai/awesome-speaker-embedding) -- Curated resource list
- [Online Speaker Clustering](https://github.com/sholokhovalexey/online-speaker-clustering) -- Streaming clustering
- [Resemblyzer + Qdrant Pattern](https://codingwithcody.com/2025/04/02/containerized-voice-identification/) -- Production containerized solution
- [Cross-File DBSCAN Clustering](https://medium.com/@sapkotabinit2002/speaker-identification-and-clustering-using-pyannote-dbscan-and-cosine-similarity-dfa08b5b2a24)

### OpenSearch & Vector Search
- [OpenSearch Vector Search Documentation](https://docs.opensearch.org/latest/vector-search/)
- [OpenSearch ML Commons Algorithms](https://docs.opensearch.org/latest/ml-commons-plugin/algorithms/)
- [OpenSearch kNN Query Documentation](https://docs.opensearch.org/latest/query-dsl/specialized/k-nn/index/)
- [OpenSearch GPU-Accelerated Vector Search](https://opensearch.org/blog/gpu-accelerated-vector-search-opensearch-new-frontier/)
- [Uber Billion-Scale Vector Search with OpenSearch](https://www.uber.com/blog/powering-billion-scale-vector-search-with-opensearch/)
- [AWS OpenSearch kNN at Billion Scale](https://aws.amazon.com/blogs/big-data/choose-the-k-nn-algorithm-for-your-billion-scale-use-case-with-opensearch/)
- [OpenSearch Efficient kNN Filtering](https://docs.opensearch.org/latest/vector-search/filter-search-knn/efficient-knn-filtering/)
- [OpenSearch Indexing Performance Tuning](https://docs.opensearch.org/latest/vector-search/performance-tuning-indexing/)

### Clustering Algorithms
- [HDBSCAN Documentation](https://hdbscan.readthedocs.io/en/latest/comparing_clustering_algorithms.html)
- [Spectral Clustering Robustness for Diarization (arXiv)](https://arxiv.org/html/2403.14286v1)
- [Block-Online Speaker Diarization (Springer)](https://link.springer.com/article/10.1186/s13636-024-00382-2)
- [Vakyansh Speaker Clustering](https://open-speech-ekstep.github.io/speaker_clustering/)
- [Voice Reproducibility: Room Acoustics and Microphones (PMC)](https://pmc.ncbi.nlm.nih.gov/articles/PMC6529301/)

### Privacy & Compliance
- [Google Pixel Recorder Privacy Approach](https://support.google.com/pixelphone/answer/16269004?hl=en)
- [Picovoice Speaker Recognition vs Identification](https://picovoice.ai/blog/speaker-diarization-vs-speaker-recognition-identification/)
- [Voice Biometrics (Parloa)](https://www.parloa.com/knowledge-hub/voice-biometrics/)
