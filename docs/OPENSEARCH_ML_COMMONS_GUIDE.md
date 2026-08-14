# OpenSearch ML Commons Integration Guide

## Overview

### What is ML Commons?

ML Commons is an OpenSearch plugin that enables machine learning (ML) model management and inference directly within the OpenSearch cluster. Instead of generating embeddings on the client side, ML Commons allows you to:

- **Register** ML models from HuggingFace or local files
- **Deploy** models into OpenSearch's memory for fast inference
- **Generate embeddings** server-side using native neural search
- **Create ingest pipelines** that automatically embed documents during indexing

### Why ML Commons Over Client-Side Embeddings?

| Aspect | Client-Side | ML Commons (Server-Side) |
|--------|-------------|--------------------------|
| **Latency** | High (embed before indexing) | Low (embed during indexing) |
| **Scalability** | Limited to single client | Distributed across cluster |
| **Memory Usage** | Client memory + network | Server memory only |
| **CPU Usage** | Client CPU | Server CPU (offloaded) |
| **Consistency** | Potential drift between versions | Single source of truth |
| **Maintenance** | Update multiple clients | Update once in OpenSearch |

### How It Fits in the Architecture

```
Frontend (Svelte)
    ↓
Backend (FastAPI)
    ├── Text search queries
    ├── Vector search queries
    └── Embedding requests
        ↓
OpenSearch Cluster
    ├── Full-text search (BM25)
    ├── Vector search (kNN)
    ├── ML Commons Plugin
    │   ├── Model Registry
    │   ├── Model Inference
    │   └── Ingest Pipelines
    └── Indices (transcripts, speakers, summaries)
```

### Benefits of Server-Side Embeddings

1. **Reduced Latency**: Embeddings computed server-side, eliminating round-trip network overhead
2. **Lower Client Load**: Backend no longer needs embedding libraries installed
3. **Automatic Indexing**: Ingest pipelines embed documents on insertion
4. **Model Centralization**: Single model version for consistency
5. **Easy Model Updates**: Switch models without redeploying clients
6. **Offline Support**: Can register models from local files in air-gapped environments

---

## Architecture Overview

### System Components

```
┌─────────────────────────────────────────────────────────────────┐
│                         Frontend                                 │
│                  (Svelte SPA, TypeScript)                       │
└────────────────────────┬────────────────────────────────────────┘
                         │ HTTP/WebSocket
                         ↓
┌─────────────────────────────────────────────────────────────────┐
│                       Backend                                    │
│                     (FastAPI)                                   │
│  ┌────────────────────────────────────────────────────────────┐ │
│  │ opensearch_service.py                                      │ │
│  │  - Index transcripts with embeddings                       │ │
│  │  - Search transcripts (text + semantic)                    │ │
│  │  - Manage speaker embeddings                               │ │
│  │  - Hybrid search with RRF                                  │ │
│  └────────────────────────────────────────────────────────────┘ │
│  ┌────────────────────────────────────────────────────────────┐ │
│  │ ml_model_service.py                                        │ │
│  │  - Register/deploy models                                  │ │
│  │  - Manage ingest pipelines                                 │ │
│  │  - Model lifecycle management                              │ │
│  │  - Support local & remote models                           │ │
│  └────────────────────────────────────────────────────────────┘ │
└────────────────────────┬────────────────────────────────────────┘
                         │ HTTP
                         ↓
┌─────────────────────────────────────────────────────────────────┐
│              OpenSearch 3.4.0 Cluster                           │
│  ┌────────────────────────────────────────────────────────────┐ │
│  │ ML Commons Plugin                                          │ │
│  │  ┌──────────────────────────────────────────────────────┐ │ │
│  │  │ Model Registry & Management                         │ │ │
│  │  │  - all-MiniLM-L6-v2 (384-dim, English)             │ │ │
│  │  │  - paraphrase-multilingual-* (50+ languages)       │ │ │
│  │  │  - all-mpnet-base-v2 (768-dim, balanced)           │ │ │
│  │  │  - DistilUSE/DistilRoBERTa (best quality)          │ │ │
│  │  └──────────────────────────────────────────────────────┘ │ │
│  │  ┌──────────────────────────────────────────────────────┐ │ │
│  │  │ Inference Engine (CPU or GPU)                       │ │ │
│  │  │  - Text Embedding Processor                         │ │ │
│  │  │  - Model Deployment Management                      │ │ │
│  │  │  - Caching & Optimization                           │ │ │
│  │  └──────────────────────────────────────────────────────┘ │ │
│  │  ┌──────────────────────────────────────────────────────┐ │ │
│  │  │ Ingest Pipelines                                    │ │ │
│  │  │  - transcript-neural-ingest                         │ │ │
│  │  │  - Auto-embed on document insertion                 │ │ │
│  │  │  - Configurable source/target fields                │ │ │
│  │  └──────────────────────────────────────────────────────┘ │ │
│  └────────────────────────────────────────────────────────────┘ │
│  ┌────────────────────────────────────────────────────────────┐ │
│  │ Search Capabilities                                        │ │
│  │  ┌──────────────────────────────────────────────────────┐ │ │
│  │  │ Full-Text Search (BM25)                             │ │ │
│  │  │  - Keyword matching with fuzziness                  │ │ │
│  │  │  - Speaker & tag filtering                          │ │ │
│  │  └──────────────────────────────────────────────────────┘ │ │
│  │  ┌──────────────────────────────────────────────────────┐ │ │
│  │  │ Vector Search (kNN with HNSW)                       │ │ │
│  │  │  - Semantic similarity search                       │ │ │
│  │  │  - Speaker matching                                 │ │ │
│  │  │  - Profile matching                                 │ │ │
│  │  └──────────────────────────────────────────────────────┘ │ │
│  │  ┌──────────────────────────────────────────────────────┐ │ │
│  │  │ Hybrid Search with RRF                              │ │ │
│  │  │  - Reciprocal Rank Fusion merging                   │ │ │
│  │  │  - Combined text + semantic results                 │ │ │
│  │  └──────────────────────────────────────────────────────┘ │ │
│  └────────────────────────────────────────────────────────────┘ │
│  ┌────────────────────────────────────────────────────────────┐ │
│  │ Indices                                                    │ │
│  │  - transcripts: Full transcripts with embeddings          │ │
│  │  - speakers: Speaker embeddings for voice fingerprinting  │ │
│  │  - transcript_chunks: chat/RAG chunk + digest plane       │ │
│  │  - topic_suggestions: Auto-generated topics              │ │
│  └────────────────────────────────────────────────────────────┘ │
└─────────────────────────────────────────────────────────────────┘
```

### Data Flow for Embedding Generation

#### During Document Indexing
```
1. Transcript ready for indexing
   ↓
2. Backend calls index_transcript() with text content
   ↓
3. Document sent to OpenSearch with pipeline: transcript-neural-ingest
   ↓
4. OpenSearch ingest pipeline intercepts document
   ↓
5. text_embedding processor extracts content field
   ↓
6. ML Commons embedding model generates vector
   ↓
7. Vector stored in embedding field
   ↓
8. Document indexed with full-text + vector search capability
```

#### During Search
```
1. User enters search query
   ↓
2. Backend processes query (opensearch_service.search_transcripts)
   ↓
3. If semantic search enabled:
   a. Query text sent to ML Commons via ingest pipeline
   b. Vector generated for query embedding
   c. kNN search performed with vector
   ↓
4. Text search also performed (BM25)
   ↓
5. Results merged using RRF (Reciprocal Rank Fusion)
   ↓
6. Combined results returned to frontend
```

---

## Model Registration Process

### How Models Are Discovered

OpenSearch ML Commons supports two modes for model discovery:

1. **Remote Registration** (Online, from HuggingFace)
   - Models downloaded from HuggingFace Hub
   - Requires internet connectivity
   - Automatic download on first use
   - Format: `huggingface/sentence-transformers/{model-name}`

2. **Local Registration** (Offline, from filesystem)
   - Models pre-downloaded and mounted in container
   - Volume: `${MODEL_CACHE_DIR}/opensearch-ml:/ml-models`
   - Used for air-gapped deployments
   - File naming convention: `sentence-transformers_{model-name}-{version}-torch_script.zip`

### Model Registration Flow

```
1. Check if model already registered
   ↓
2a. If NOT registered:
   ├─ Check for local model file (air-gapped)
   ├─ If local: Register from file:// URL
   └─ If not local: Register from HuggingFace
   ↓
2b. If registered but not deployed:
   └─ Deploy model to memory
   ↓
3. Model ready for inference (text_embedding processor)
```

### Configuration in ML Commons

When registering a model, ML Commons requires:

```python
{
    "name": "huggingface/sentence-transformers/all-MiniLM-L6-v2",
    "version": "1.0.1",
    "model_format": "TORCH_SCRIPT",  # Other: ONNX, SAFETENSORS
    "url": "https://huggingface.co/models/..." or "file://..."
}
```

### Supported Embedding Models (6 Tiers)

OpenTranscribe supports 6 state-of-the-art embedding models, organized by quality and speed:

#### Fast Tier (384 dimensions) - Low Latency

**all-MiniLM-L6-v2** (Default)
- **Languages**: English only
- **Size**: 80 MB
- **Speed**: Fastest
- **Use**: Keyword-heavy searches, high-throughput scenarios
- **Quality**: Good for English, not multilingual
- **Config**: `huggingface/sentence-transformers/all-MiniLM-L6-v2`

**paraphrase-multilingual-MiniLM-L12-v2**
- **Languages**: 50+ languages
- **Size**: 420 MB
- **Speed**: Fast with multilingual support
- **Use**: Multilingual applications, budget-conscious deployments
- **Quality**: Good for 50+ languages with reasonable speed
- **Config**: `huggingface/sentence-transformers/paraphrase-multilingual-MiniLM-L12-v2`

#### Balanced Tier (768 dimensions) - Speed + Quality

**all-mpnet-base-v2**
- **Languages**: English only
- **Size**: 420 MB
- **Speed**: Moderate
- **Use**: Semantic search with better accuracy than MiniLM
- **Quality**: Excellent English semantic understanding
- **Config**: `huggingface/sentence-transformers/all-mpnet-base-v2`

**paraphrase-multilingual-mpnet-base-v2**
- **Languages**: 50+ languages
- **Size**: 1100 MB
- **Speed**: Moderate
- **Use**: High-quality multilingual semantic search
- **Quality**: Best multilingual semantic understanding
- **Config**: `huggingface/sentence-transformers/paraphrase-multilingual-mpnet-base-v2`

#### Best Quality Tier - Maximum Accuracy

**all-distilroberta-v1**
- **Languages**: English only
- **Size**: 290 MB
- **Speed**: Moderate
- **Use**: Maximum English retrieval quality
- **Quality**: Best English semantic understanding
- **Config**: `huggingface/sentence-transformers/all-distilroberta-v1`

**distiluse-base-multilingual-cased-v1**
- **Languages**: 15 major languages
- **Size**: 480 MB
- **Speed**: Moderate
- **Use**: Best multilingual quality for major languages
- **Quality**: Superior accuracy for 15 common languages
- **Config**: `huggingface/sentence-transformers/distiluse-base-multilingual-cased-v1`

### Custom Model Support

To add custom models:

1. **Download** model from HuggingFace and convert to TorchScript format
2. **Package** as ZIP file following OpenSearch naming: `{model}-{version}-torch_script.zip`
3. **Place** in `${MODEL_CACHE_DIR}/opensearch-ml/` directory
4. **Register** using `ml_model_service.register_model_from_local(model_name)`

Example adding a custom model:

```python
from app.services.search.ml_model_service import get_ml_model_service

service = get_ml_model_service()

# Register from local file
model_id = service.register_model_from_local(
    "huggingface/sentence-transformers/my-custom-model"
)

# Deploy for inference
if model_id:
    service.deploy_model(model_id)
```

---

## Embedding Generation API

### OpenSearch Text Embedding Processor

The `text_embedding` processor is the core mechanism for embedding generation:

```json
{
  "text_embedding": {
    "model_id": "model-id-from-registration",
    "field_map": {
      "source_field": "target_field"
    }
  }
}
```

### Ingest Pipeline Structure

```json
{
  "processors": [
    {
      "text_embedding": {
        "model_id": "xxxxxxxxxxxxxxxx",
        "field_map": {
          "content": "embedding"
        }
      }
    }
  ]
}
```

### Request/Response Format

#### Indexing with Ingest Pipeline

**Request**:
```python
{
    "file_id": 1,
    "file_uuid": "abc-123",
    "user_id": 42,
    "content": "This is the transcript text...",
    "title": "Meeting 2025-01-15",
    "speakers": ["Alice", "Bob"],
    "tags": ["important"]
}
```

**Processing**:
1. Pipeline intercepts request
2. Extracts `content` field
3. Generates embedding via model
4. Adds `embedding` field

**Stored Document**:
```python
{
    "file_id": 1,
    "file_uuid": "abc-123",
    "user_id": 42,
    "content": "This is the transcript text...",
    "title": "Meeting 2025-01-15",
    "speakers": ["Alice", "Bob"],
    "tags": ["important"],
    "embedding": [0.234, -0.156, 0.789, ...]  # 384-dim vector
}
```

### Batch Embedding Generation

For efficient bulk indexing:

```python
from app.services.opensearch_service import bulk_add_speaker_embeddings

embeddings_data = [
    {
        "speaker_id": 1,
        "speaker_uuid": "uuid-1",
        "user_id": 42,
        "name": "Alice",
        "embedding": [0.234, -0.156, ...]  # Pre-generated
    },
    {
        "speaker_id": 2,
        "speaker_uuid": "uuid-2",
        "user_id": 42,
        "name": "Bob",
        "embedding": [0.445, -0.267, ...]
    }
]

bulk_add_speaker_embeddings(embeddings_data)
```

### Error Handling

Embedding generation errors are handled gracefully:

1. **Missing Model**: If model not deployed, pipeline fails with clear error
2. **Invalid Format**: Non-text fields rejected by processor
3. **Size Limits**: Very large texts may be truncated
4. **Timeout**: Long-running embeddings may timeout (configurable)

Error response example:
```json
{
  "error": {
    "type": "parsing_exception",
    "reason": "Unknown processor type [text_embedding]",
    "line": 2,
    "col": 21
  }
}
```

---

## Batch Indexing Implementation

### Batching Strategy

OpenTranscribe uses intelligent batching for efficient indexing:

```python
# Configuration
BATCH_SIZE = 100  # Documents per batch
MAX_BULK_SIZE = 10_000  # 10MB max bulk request size
PROGRESS_UPDATE_INTERVAL = 1000  # Every 1000 docs

# Batching algorithm
documents_to_index = load_all_documents()  # N documents
for batch_offset in range(0, len(documents), BATCH_SIZE):
    batch = documents_to_index[batch_offset:batch_offset + BATCH_SIZE]
    bulk_index(batch)

    # Report progress to frontend via WebSocket
    if batch_offset % PROGRESS_UPDATE_INTERVAL == 0:
        notify_progress(offset=batch_offset, total=len(documents))
```

### How Transcripts Are Indexed

1. **Transcript Processing** (Backend)
   ```python
   from app.services.opensearch_service import index_transcript

   # After transcription completes
   index_transcript(
       file_id=file.id,
       file_uuid=file.uuid,
       user_id=file.user_id,
       transcript_text=full_transcript_text,
       speakers=[s.name for s in speakers],
       title=file.title,
       tags=file.tags,
       # embedding=None  # Let ingest pipeline handle it
   )
   ```

2. **Pipeline Processing** (OpenSearch)
   - Document arrives at OpenSearch
   - Ingest pipeline intercepts
   - `text_embedding` processor runs
   - Vector added to document
   - Document indexed

3. **Search Capability**
   - BM25 full-text search on `content` field
   - kNN vector search on `embedding` field
   - Both available for hybrid search

### Performance Considerations

| Factor | Impact | Optimization |
|--------|--------|---------------|
| Batch Size | Too small = overhead, too large = memory | 100-1000 docs |
| Model Size | Larger models = slower embedding | Use appropriate tier |
| Document Size | Larger docs = longer embedding time | Pre-chunk if needed |
| Network Latency | Network round-trips | Batch requests |
| Disk I/O | Index writing | Use bulk API |

### Progress Tracking

Progress tracking sends WebSocket notifications:

```python
# Message format
{
    "type": "reindex_progress",
    "indexed": 1000,
    "total": 5000,
    "percentage": 20,
    "status": "Indexing documents..."
}
```

Frontend can display progress bar with real-time updates.

---

## v0.4.0 Critical Bug Fixes

### Bug 1: Hybrid Search Was Non-Functional (ArrayIndexOutOfBoundsException)

**Symptom**: All searches silently fell back to BM25-only with zero semantic contribution. No errors visible to users.

**Root cause**: OpenSearch 3.4 throws a Java `ArrayIndexOutOfBoundsException` when a `hybrid` query is combined with all three of: `aggs` (cardinality aggregation), `collapse`, and an RRF `search_pipeline`. The exception is swallowed internally; the response returns BM25 results only.

**Fix**: Removed `aggs` from the hybrid query body in `_build_collapsed_search_body()`. The `total_files` count is now computed from `len(outer_hits)`. The BM25-only path retains `aggs` (no crash). This fix restored full hybrid search functionality — a search like `"pytorch"` went from returning 1 result to 83 results.

**Impact**: If you were running v0.3.x, all your searches were keyword-only regardless of model configuration. Upgrading to v0.4.0 restores semantic search.

### Bug 2: Cosine Score Bias (All Similarity Scores Were Wrong)

**Symptom**: Speaker matching and profile matching scores were systematically inflated. A raw cosine similarity of 0.50 appeared as 0.75.

**Root cause**: OpenSearch's `cosinesimil` space type for kNN indices returns `(1 + cosine) / 2`, not the raw cosine value. This is a normalization to keep scores in the range [0, 1], but the code was treating returned scores as raw cosine values directly.

**Fix**: Convert at every kNN score read: `raw_cosine = 2.0 * hit["_score"] - 1.0`. Fixed at 8 locations across `opensearch_service.py`, `speaker_matching_service.py`, `profile_embedding_service.py`, `smart_speaker_suggestion_service.py`, and `similarity_service.py`.

**Impact**: Thresholds (0.75 auto-accept, 0.50 suggest) now correctly apply to actual cosine similarity values.

### Speaker Index Alias Architecture

As of v0.4.0, the `speakers` index name is an **alias**, not a concrete index:

| Alias | Points to | Embedding model | Dimensions |
|-------|-----------|-----------------|------------|
| `speakers` | `speakers_v3` or `speakers_v4` | pyannote/embedding or WeSpeaker | 512 / 256 |

On startup, `ensure_indices_exist()` runs `migrate_to_alias_based_indices()` which performs an atomic alias swap if needed. All read/write operations use the `speakers` alias — concrete index names are an implementation detail.

---

## Hybrid Search (BM25 + Neural) with RRF

### How Hybrid Search Works

Hybrid search combines two complementary search methods:

1. **BM25 Full-Text Search** (Keyword-based)
   - Matches query terms in text
   - Handles exact phrases
   - Fast, language-aware
   - Good for known keywords

2. **Neural/Vector Search** (Semantic-based)
   - Matches semantic meaning
   - Handles paraphrasing
   - Slower, language-agnostic
   - Good for concept search

### BM25 Full-Text Search

BM25 (Best Match 25) is a probabilistic ranking function that considers:

```
Score = IDF(query_term) × (f(query_term, doc) × (k₁ + 1)) /
        (f(query_term, doc) + k₁ × (1 - b + b × (doc_length / avg_doc_length)))

Where:
- IDF = Inverse Document Frequency
- f = Term frequency in document
- k₁ = Term frequency saturation point (default: 1.2)
- b = Field length normalization (default: 0.75)
```

**Example BM25 Query**:
```python
{
    "query": {
        "bool": {
            "must": [
                {"match": {
                    "content": {
                        "query": "quarterly earnings report",
                        "fuzziness": "AUTO"
                    }
                }},
                {"term": {"user_id": 42}}
            ],
            "filter": [
                {"term": {"speakers": "CEO"}}
            ]
        }
    }
}
```

### Neural/Vector Search

Vector search uses k-Nearest Neighbors (kNN) with HNSW indexing:

```python
{
    "size": 10,
    "query": {
        "knn": {
            "embedding": {
                "vector": [0.234, -0.156, 0.789, ...],  # Query embedding
                "k": 10,
                "filter": {
                    "bool": {
                        "must": [
                            {"term": {"user_id": 42}}
                        ]
                    }
                }
            }
        }
    }
}
```

**Configuration**:
```python
# Index creation
"embedding": {
    "type": "knn_vector",
    "dimension": 384,  # Based on model
    "method": {
        "name": "hnsw",
        "space_type": "cosinesimil",
        "engine": "lucene",
        "parameters": {
            "ef_construction": 128,  # Index efficiency
            "m": 24                   # Connections per node
        }
    }
}
```

### RRF (Reciprocal Rank Fusion)

RRF combines ranked lists from BM25 and vector search:

#### Algorithm

```
RRF(doc) = Σ 1 / (k + rank_of_doc_in_list_i)

Where:
- k = constant (default: 30, configurable via SEARCH_RRF_RANK_CONSTANT)
- rank_i = Document's position in each ranked list
- Σ = Sum across all ranked lists
```

#### Example

**Scenario**: Search for "AI transcription benefits"

**BM25 Results**:
1. Doc A (score: 8.5)
2. Doc C (score: 6.2)
3. Doc B (score: 5.1)

**Vector Search Results**:
1. Doc B (score: 0.92)
2. Doc A (score: 0.89)
3. Doc D (score: 0.81)

**RRF Fusion** (k=30, v0.4.0 default):
```
Doc A: 1/(30+1) + 1/(30+2) = 0.0323 + 0.0313 = 0.0635 (Highest)
Doc B: 1/(30+3) + 1/(30+1) = 0.0303 + 0.0323 = 0.0626
Doc C: 1/(30+2)           = 0.0313
Doc D: 1/(30+3)           = 0.0303
```

**Final Ranking**: Doc A > Doc B > Doc C > Doc D

### Implementation in OpenTranscribe

```python
def search_transcripts(
    query: str,
    user_id: int,
    speaker: str | None = None,
    tags: list[str] | None = None,
    limit: int = 10,
    use_semantic: bool = True
) -> list[dict]:
    """
    Hybrid search combining BM25 + vector search with RRF.
    """
    # Build must conditions (filters)
    must_conditions = [
        {"term": {"user_id": user_id}}
    ]

    if query:
        must_conditions.append({
            "match": {"content": {
                "query": query,
                "fuzziness": "AUTO"
            }}
        })

    if speaker:
        must_conditions.append({"term": {"speakers": speaker}})

    if tags:
        must_conditions.append({"terms": {"tags": tags}})

    # BM25 search
    search_body = {
        "query": {"bool": {"must": must_conditions}},
        "size": limit
    }

    # Add neural/hybrid search if enabled
    # In v0.4.0, embeddings are generated server-side by OpenSearch ML Commons.
    # The hybrid query is executed via the transcript-hybrid-search search pipeline
    # (RRF), which merges BM25 and kNN results automatically.
    if use_semantic and query:
        search_body = {
            "query": {
                "hybrid": {
                    "queries": [
                        {"bool": {"must": must_conditions}},
                        {"neural": {
                            "embedding": {
                                "query_text": query,
                                "model_id": active_model_id,
                                "k": limit
                            }
                        }}
                    ]
                }
            },
            "size": limit
        }

    # Execute search (hybrid pipeline applies RRF automatically)
    response = opensearch_client.search(
        index="transcripts",
        body=search_body,
        params={"search_pipeline": "transcript-hybrid-search"}
    )

    return response["hits"]["hits"]
```

### Weight Tuning

To favor one search method over another:

```python
# Emphasize keyword matching (more BM25)
search_body = {
    "query": {
        "bool": {
            "must": [
                {"match": {"content": {
                    "query": query,
                    "fuzziness": "AUTO",
                    "boost": 2.0  # Double weight for text match
                }}}
            ],
            "should": [{
                "knn": {
                    "embedding": {
                        "vector": query_embedding,
                        "k": limit
                    }
                }
            }]
        }
    }
}

# Emphasize semantic meaning (more vector)
search_body = {
    "query": {
        "bool": {
            "should": [
                {
                    "match": {"content": {
                        "query": query,
                        "fuzziness": "AUTO"
                    }}
                },
                {
                    "knn": {
                        "embedding": {
                            "vector": query_embedding,
                            "k": limit,
                            "boost": 2.0  # Double weight for semantic
                        }
                    }
                }
            ]
        }
    }
}
```

---

## Backend Service (opensearch_service.py)

### Service Overview

The `opensearch_service.py` module provides a complete abstraction over OpenSearch operations:

```
opensearch_service.py
├── Client Management
│   ├── get_opensearch_client()
│   └── ensure_indices_exist()
├── Index Management
│   ├── create_speaker_index_v4()
│   └── Transcript/speaker index creation
├── Transcript Operations
│   ├── index_transcript()
│   ├── update_transcript_title()
│   └── search_transcripts()
├── Speaker Embedding Operations
│   ├── add_speaker_embedding()
│   ├── add_speaker_embedding_v4()
│   ├── bulk_add_speaker_embeddings()
│   └── find_matching_speaker()
├── Profile Embedding Operations
│   ├── store_profile_embedding()
│   ├── update_profile_embedding()
│   ├── find_matching_profiles()
│   └── remove_profile_embedding()
├── Collection Management
│   ├── update_speaker_collections()
│   ├── move_speaker_to_profile_collection()
│   ├── bulk_update_collection_assignments()
│   └── get_speakers_in_collection()
├── Utility Functions
│   ├── find_speaker_across_media()
│   ├── merge_speaker_embeddings()
│   ├── cleanup_orphaned_embeddings()
│   ├── get_speaker_embedding()
│   └── get_profile_embedding()
└── Maintenance
    └── cleanup_orphaned_speaker_embeddings()
```

### Key Methods and Functions

#### Transcript Indexing

```python
def index_transcript(
    file_id: int,
    file_uuid: str,
    user_id: int,
    transcript_text: str,
    speakers: list[str],
    title: str,
    tags: list[str] | None = None,
    embedding: list[float] | None = None,
):
    """Index a transcript in OpenSearch.

    The ingest pipeline automatically generates embeddings from the text.
    If embedding is provided, it's stored with the document.
    """
```

#### Transcript Search

```python
def search_transcripts(
    query: str,
    user_id: int,
    speaker: str | None = None,
    tags: list[str] | None = None,
    limit: int = 10,
    use_semantic: bool = True
) -> list[dict]:
    """Search for transcripts using hybrid BM25 + vector search.

    Returns:
        List of matching documents with snippets and metadata.
    """
```

#### Speaker Embedding Operations

```python
def find_matching_speaker(
    embedding: list[float],
    user_id: int,
    threshold: float = 0.5,
    collection_ids: list[int] | None = None,
    exclude_speaker_ids: list[int] | None = None,
) -> dict | None:
    """Find a matching speaker for a given embedding.

    Args:
        embedding: Speaker voice embedding vector
        user_id: User ID for filtering
        threshold: Minimum similarity score (0-1)
        collection_ids: Optional filter by collections
        exclude_speaker_ids: Exclude specific speakers

    Returns:
        Match result with speaker ID and confidence score
    """
```

#### Batch Operations

```python
def bulk_add_speaker_embeddings(embeddings_data: list[dict]):
    """Efficiently bulk index multiple speaker embeddings.

    Much faster than individual index_transcript calls.
    Combines multiple documents into single bulk request.
    """

def batch_find_matching_speakers(
    embeddings: list[dict],
    user_id: int,
    threshold: float = 0.5,
    max_candidates: int = 5,
) -> list[dict]:
    """Find matches for multiple embeddings in one operation.

    Uses OpenSearch's msearch API for efficiency.
    """
```

### Configuration Handling

Configuration is centralized in `app/core/config.py`:

```python
class Settings:
    # OpenSearch connection
    OPENSEARCH_HOST: str = "localhost"
    OPENSEARCH_PORT: str = "9200"
    OPENSEARCH_USER: str = "admin"
    OPENSEARCH_PASSWORD: str = "admin"
    OPENSEARCH_VERIFY_CERTS: bool = False

    # Index names
    OPENSEARCH_TRANSCRIPT_INDEX: str = "transcripts"
    OPENSEARCH_SPEAKER_INDEX: str = "speakers"

    # Neural search
    OPENSEARCH_NEURAL_SEARCH_ENABLED: bool = True
    OPENSEARCH_NEURAL_MODEL: str = "huggingface/sentence-transformers/all-MiniLM-L6-v2"
    OPENSEARCH_NEURAL_PIPELINE: str = "transcript-neural-ingest"
    OPENSEARCH_SEARCH_PIPELINE: str = "transcript-hybrid-search"
```

Access in code:

```python
from app.core.config import settings

host = settings.OPENSEARCH_HOST
port = settings.OPENSEARCH_PORT
transcript_index = settings.OPENSEARCH_TRANSCRIPT_INDEX
```

### Error Handling Strategies

#### Connection Errors

```python
try:
    opensearch_client = OpenSearch(hosts=[...])
except ConnectionError as e:
    logger.error(f"Configuration error: {e}")
    opensearch_client = None

# Later: check before operations
if not opensearch_client:
    logger.warning("OpenSearch not available")
    return []
```

#### Document Errors

```python
try:
    if embedding is None:
        logger.error("Cannot index: embedding is None")
        return

    if not isinstance(embedding, list) or len(embedding) == 0:
        logger.error("Invalid embedding format")
        return

    response = opensearch_client.index(...)
except Exception as e:
    logger.error(f"Error indexing: {e}")
```

#### Graceful Degradation

```python
# Test environment: return mock results
if os.environ.get("SKIP_OPENSEARCH"):
    return [{
        "file_id": 1,
        "title": "Mock Result",
        "snippet": "Testing..."
    }]

# Fallback: text search only
search_body = {
    "query": {"bool": {"must": [
        {"match": {"content": query}}
    ]}}
}
```

---

## ML Model Lifecycle Management

### Model Deployment

#### Deployment Checklist

```python
from app.services.search.ml_model_service import get_ml_model_service

service = get_ml_model_service()

# 1. Configure ML Commons settings
service.configure_ml_settings()  # Enable ML on all nodes

# 2. Register model (from local or remote)
model_id = service.ensure_model_deployed(
    "huggingface/sentence-transformers/all-MiniLM-L6-v2"
)

# 3. Create ingest pipeline
service.create_ingest_pipeline(
    pipeline_id="transcript-neural-ingest",
    model_id=model_id,
    source_field="content",
    target_field="embedding"
)

# 4. Verify deployment
status = service.get_model_status(model_id)
print(f"Model {model_id} deployed: {status['deployed']}")
```

#### Automatic Deployment

Models are automatically deployed on backend startup via initialization scripts:

```python
# In startup event handler
async def startup_event():
    from app.services.search.ml_model_service import get_ml_model_service
    from app.core.config import settings

    service = get_ml_model_service()

    # Ensure default model is deployed
    model_id = service.ensure_model_deployed(
        settings.OPENSEARCH_NEURAL_MODEL
    )

    if model_id:
        logger.info(f"Default model deployed: {model_id}")
        service.set_active_model_id(model_id)
```

### Model Versioning

Models are tracked in the database:

```python
# Setting storage
settings_service.set_setting(
    "search.opensearch_model_id",
    model_id,
    "OpenSearch ML model ID for neural search"
)

settings_service.set_setting(
    "search.opensearch_model_name",
    "all-MiniLM-L6-v2",
    "Model name for display"
)
```

### Model Updates

#### Process for Switching Models

```python
# 1. Deploy new model
new_model_id = service.ensure_model_deployed(
    "huggingface/sentence-transformers/all-mpnet-base-v2"
)

# 2. Update ingest pipeline to use new model
service.update_ingest_pipeline_model(
    pipeline_id="transcript-neural-ingest",
    new_model_id=new_model_id
)

# 3. Update settings
service.set_active_model_id(new_model_id)

# 4. Optionally re-index existing documents
# (new documents will use new model, old will keep old embedding)
```

#### Re-indexing with New Model

```python
def reindex_all_transcripts(
    old_model_id: str,
    new_model_id: str,
    batch_size: int = 100
):
    """Re-index all transcripts with new model."""

    # Query all transcripts
    query = {"query": {"match_all": {}}, "size": batch_size}

    offset = 0
    while True:
        # Get batch
        response = opensearch_client.search(
            index="transcripts",
            body={...query, "from": offset}
        )

        hits = response["hits"]["hits"]
        if not hits:
            break

        # Re-index with new pipeline
        for hit in hits:
            doc = hit["_source"]
            doc["_pipeline"] = "transcript-neural-ingest"  # Uses new model

            opensearch_client.index(
                index="transcripts",
                id=hit["_id"],
                body=doc,
                pipeline="transcript-neural-ingest"
            )

        offset += batch_size
        logger.info(f"Re-indexed {offset} documents")
```

### Rollback Procedures

#### Rollback Checklist

```python
# 1. Keep previous model deployed
# (don't undeploy old model immediately)

# 2. Monitor new model performance
metrics = {
    "search_latency": measure_latency(),
    "result_quality": measure_quality(),
    "error_rate": measure_errors()
}

# 3. If issues detected, rollback
if metrics["error_rate"] > THRESHOLD:
    logger.warning("Rolling back to previous model")

    # Update pipeline to use old model
    service.update_ingest_pipeline_model(
        pipeline_id="transcript-neural-ingest",
        new_model_id=old_model_id
    )

    # Update settings
    service.set_active_model_id(old_model_id)

    # Undeploy new model (after confirmation)
    service.undeploy_model(new_model_id)
```

### Health Checks

```python
def check_model_health() -> bool:
    """Verify model is ready for inference."""
    service = get_ml_model_service()

    # Get active model
    model_id = service.get_active_model_id()
    if not model_id:
        return False

    # Check status
    status = service.get_model_status(model_id)
    if not status.get("deployed"):
        logger.error("Model not deployed")
        return False

    # Test inference with sample text
    try:
        # This would normally be done via search
        # Using a simple query to verify model works
        result = opensearch_client.search(
            index="transcripts",
            body={
                "query": {
                    "knn": {
                        "embedding": {
                            "vector": [0] * 384,
                            "k": 1
                        }
                    }
                },
                "size": 1
            }
        )
        return result is not None
    except Exception as e:
        logger.error(f"Model health check failed: {e}")
        return False
```

---

## Performance Tuning

### Batch Size Optimization

#### Finding Optimal Batch Size

```python
import time
import statistics

def benchmark_batch_sizes():
    """Test different batch sizes to find optimal."""
    batch_sizes = [10, 50, 100, 500, 1000]
    results = {}

    for batch_size in batch_sizes:
        times = []

        for _ in range(5):  # 5 trials
            start = time.time()
            bulk_index_batch(batch_size)
            elapsed = time.time() - start
            times.append(elapsed)

        results[batch_size] = {
            "mean": statistics.mean(times),
            "stddev": statistics.stdev(times),
            "docs_per_sec": (batch_size * 5) / sum(times)
        }

    # Find best throughput
    best = max(results.items(), key=lambda x: x[1]["docs_per_sec"])
    logger.info(f"Optimal batch size: {best[0]} ({best[1]['docs_per_sec']:.0f} docs/sec)")
```

| Batch Size | Network Overhead | Memory | Speed | Optimal |
|------------|-----------------|--------|-------|---------|
| 10 | High | Low | Slow | Poor |
| **100** | **Balanced** | **Good** | **Good** | **Best** |
| 500 | Low | Higher | Faster | Good |
| 1000 | Very Low | High | Fastest | Risk |

**Recommendation**: Start with 100, adjust based on available memory.

### Memory Management

#### Monitor Memory Usage

```python
import psutil

def monitor_opensearch_memory():
    """Monitor OpenSearch process memory."""
    process = psutil.Process(opensearch_pid)

    memory = process.memory_info()
    percent = process.memory_percent()

    logger.info(f"OpenSearch memory: {memory.rss / 1024**2:.0f}MB ({percent:.1f}%)")

    if percent > 80:
        logger.warning("OpenSearch using >80% of available memory")
```

#### Model Memory Requirements

| Model | Dimensions | Size | Memory (Deployed) |
|-------|-----------|------|------------------|
| all-MiniLM-L6-v2 | 384 | 80 MB | 400-500 MB |
| paraphrase-multilingual-MiniLM-L12-v2 | 384 | 420 MB | 500-600 MB |
| all-mpnet-base-v2 | 768 | 420 MB | 800-1000 MB |
| paraphrase-multilingual-mpnet-base-v2 | 768 | 1100 MB | 1200-1500 MB |
| all-distilroberta-v1 | 768 | 290 MB | 700-900 MB |
| distiluse-base-multilingual-cased-v1 | 512 | 480 MB | 600-800 MB |

### Query Performance

#### Query Optimization Tips

```python
# 1. Use filters instead of must for exact matches
# Slower: must with match
{"bool": {"must": [{"match": {"speakers": "Alice"}}]}}

# Faster: filter with term
{"bool": {"filter": {"term": {"speakers": "Alice"}}}}

# 2. Use size and from for pagination
response = opensearch_client.search(
    index="transcripts",
    body={...},
    size=20,  # Results per page
    from_=0   # Starting position
)

# 3. Use _source to limit fields
{
    "_source": ["file_id", "title", "speakers"],  # Only needed fields
    "query": {...}
}

# 4. Minimize kNN k parameter
# k=100 is slower than k=10, usually not needed
{"knn": {"embedding": {"vector": [...], "k": 10}}}

# 5. Use filter in kNN for early termination
{
    "knn": {
        "embedding": {
            "vector": [...],
            "k": 10,
            "filter": {"term": {"user_id": 42}}  # Speeds up search
        }
    }
}
```

### Indexing Performance

#### Batch Request Optimization

```python
# 1. Increase batch size within memory constraints
# 100-1000 documents per batch

# 2. Use bulk API (not individual index calls)
# Bulk: 1000 docs in one request
# Individual: 1000 separate HTTP requests (100x slower)

# 3. Server-side embeddings via ingest pipeline (default in v0.4.0):
# Documents are indexed without pre-computed embeddings; the ingest
# pipeline calls ML Commons to generate embeddings during indexing.
# No client-side embedding library is needed.

# 4. Disable refresh during bulk import
bulk_body = [
    {"index": {"_index": "transcripts", "_id": "1"}},
    {"content": "...", "embedding": [...]},
    # ... more docs
]

opensearch_client.bulk(
    body=bulk_body,
    params={"refresh": False}  # Bulk refresh after complete
)
opensearch_client.indices.refresh(index="transcripts")
```

### Caching Strategies

#### Query Result Caching

```python
from functools import lru_cache
import hashlib

@lru_cache(maxsize=256)
def cached_search(
    query_hash: str,
    user_id: int,
    limit: int
) -> tuple:
    """Cache search results for 5 minutes."""
    # Actual search logic
    return tuple(results)

def search_with_cache(query: str, user_id: int) -> list:
    """Search with caching."""
    # Create cache key
    query_hash = hashlib.md5(query.encode()).hexdigest()

    # Query cache
    results = cached_search(
        query_hash=query_hash,
        user_id=user_id,
        limit=20
    )

    return list(results)

# Clear cache on document update
@lru_cache.cache_clear
def invalidate_cache():
    """Clear all cached search results."""
    pass
```

#### Model Caching

Models are cached in OpenSearch memory once deployed:

```python
# Check if model is cached (deployed)
status = service.get_model_status(model_id)

if status["deployed"]:
    # Model is in memory, fast inference
    embedding = generate_embedding(text)
else:
    # Model not deployed, slow or unavailable
    logger.warning("Model not deployed, re-deploying...")
    service.deploy_model(model_id)
```

---

## Troubleshooting

### Common Issues and Solutions

#### Issue: "Unknown processor type [text_embedding]"

**Cause**: ML Commons plugin not installed or model not deployed

**Solution**:
```python
from app.services.search.ml_model_service import get_ml_model_service

service = get_ml_model_service()

# Configure ML settings
service.configure_ml_settings()

# List models to verify ML Commons is working
models = service.list_models()
print(f"Available models: {models}")

# If no models, register one
model_id = service.register_model(
    "huggingface/sentence-transformers/all-MiniLM-L6-v2"
)
service.deploy_model(model_id)
```

#### Issue: "Model registration timed out"

**Cause**: Model download taking too long or network issues

**Solution**:
```python
# 1. Check network connectivity
curl -X GET "localhost:9200/_plugins/_ml/models/_search"

# 2. Check model registration task status
# Look at logs: docker logs opentranscribe-opensearch

# 3. Try registering from local file (if available)
model_id = service.register_model_from_local(
    "huggingface/sentence-transformers/all-MiniLM-L6-v2"
)

# 4. Increase timeout in ml_model_service.py
_REGISTRATION_MAX_WAIT = 600  # 10 minutes instead of 5
```

#### Issue: "Out of memory" errors

**Cause**: Model too large for available memory

**Solution**:
```python
# 1. Use smaller model
model_id = service.ensure_model_deployed(
    "huggingface/sentence-transformers/all-MiniLM-L6-v2"  # 384-dim
    # instead of all-mpnet-base-v2 (768-dim)
)

# 2. Increase JVM memory allocation (docker-compose.yml)
environment:
  - "OPENSEARCH_JAVA_OPTS=-Xms2g -Xmx2g"  # Increase from 1g

# 3. Undeploy unused models
service.undeploy_model(old_model_id)

# 4. Monitor memory usage
docker stats opentranscribe-opensearch
```

#### Issue: Search returns empty results

**Cause**: Index not created, wrong field names, or documents not indexed

**Solution**:
```python
# 1. Check indices exist
opensearch_client.indices.get_mapping()

# 2. Count documents in index
response = opensearch_client.count(index="transcripts")
print(f"Documents in index: {response['count']}")

# 3. Verify field names
response = opensearch_client.search(
    index="transcripts",
    body={"query": {"match_all": {}}, "size": 1}
)

if response["hits"]["hits"]:
    fields = response["hits"]["hits"][0]["_source"].keys()
    print(f"Fields in document: {fields}")

# 4. Try simple match query
response = opensearch_client.search(
    index="transcripts",
    body={"query": {"match_all": {}}, "size": 10}
)
```

#### Issue: Slow embedding generation

**Cause**: Large documents, model not deployed, or high-dimensional model selected

**Solution**:
```python
# 1. Check ingest pipeline performance via OpenSearch stats
# docker logs opentranscribe-opensearch 2>&1 | grep -i "embedding\|pipeline"

# 2. Switch to a faster (lower-dimension) model via Admin UI:
#    Settings → Search → Embedding Model → all-MiniLM-L6-v2 (384d, fastest)
#    vs all-mpnet-base-v2 (768d, slower)

# 3. Chunking strategy: transcripts are pre-chunked by OpenTranscribe
#    before indexing (~200 words/chunk). Very large single documents
#    should also be chunked before indexing to keep embeddings focused.

# 4. Ensure the ML model is deployed (not just registered)
from app.services.search.ml_model_service import get_ml_model_service
service = get_ml_model_service()
status = service.get_model_status(service.get_active_model_id())
print(f"Model deployed: {status.get('deployed')}")
```

### Debugging Strategies

#### Enable Debug Logging

```python
import logging

# In app/core/config.py or main.py
logging.basicConfig(level=logging.DEBUG)

# For specific module
logging.getLogger("app.services.opensearch_service").setLevel(logging.DEBUG)
logging.getLogger("app.services.search.ml_model_service").setLevel(logging.DEBUG)

# View logs
docker logs -f opentranscribe-backend | grep "opensearch\|ml_commons"
```

#### Inspect OpenSearch Cluster State

```python
# Check cluster health
curl -X GET "localhost:9200/_cluster/health?pretty"

# Check node statistics
curl -X GET "localhost:9200/_nodes/stats?pretty" | jq '.nodes[] | {name, jvm}'

# Check index settings
curl -X GET "localhost:9200/transcripts/_settings?pretty"

# Check index mappings
curl -X GET "localhost:9200/transcripts/_mapping?pretty"

# Check ingest pipelines
curl -X GET "localhost:9200/_ingest/pipeline?pretty"
```

#### Test Model Inference

```bash
# Test that the ML Commons model is deployed and can generate embeddings
# via a neural search query (server-side, no client-side library needed)
curl -s -k -u admin:admin \
  "https://localhost:9200/transcripts/_search" \
  -H "Content-Type: application/json" \
  -d '{"size":1,"query":{"neural":{"embedding":{"query_text":"test","model_id":"<MODEL_ID>","k":1}}}}' \
  | python3 -m json.tool | grep '"_score"'

# Check the deployed model list
curl -s -k -u admin:admin \
  "https://localhost:9200/_plugins/_ml/models/_search" \
  -H "Content-Type: application/json" \
  -d '{"query":{"match_all":{}}}' \
  | python3 -m json.tool | grep '"model_state"'
```

### Performance Profiling

#### Profile Search Latency

```python
import time
from collections import defaultdict

class LatencyProfiler:
    def __init__(self):
        self.latencies = defaultdict(list)

    def profile_search(self, query: str, user_id: int):
        start = time.time()

        # Full search operation
        results = search_transcripts(query, user_id)

        elapsed = time.time() - start
        self.latencies["total"].append(elapsed)

        return results

    def report(self):
        for key, times in self.latencies.items():
            avg = sum(times) / len(times)
            p95 = sorted(times)[int(len(times) * 0.95)]
            p99 = sorted(times)[int(len(times) * 0.99)]

            print(f"{key}:")
            print(f"  Avg: {avg*1000:.0f}ms")
            print(f"  P95: {p95*1000:.0f}ms")
            print(f"  P99: {p99*1000:.0f}ms")
```

---

## API Reference

### Key Endpoints

#### Index Transcript
```
POST /api/transcripts/{file_id}/index
Body: {
    "transcript_text": "...",
    "speakers": ["Speaker1", "Speaker2"],
    "tags": ["tag1", "tag2"]
}
Response: {
    "indexed": true,
    "file_id": 1,
    "embedding_dimension": 384
}
```

#### Search Transcripts
```
GET /api/transcripts/search?q=query&limit=10&semantic=true
Response: {
    "results": [
        {
            "file_id": 1,
            "title": "...",
            "snippet": "...",
            "speakers": ["..."],
            "upload_time": "2025-01-15T10:00:00Z"
        }
    ],
    "total": 42
}
```

#### Find Speaker Match
```
POST /api/speakers/match
Body: {
    "embedding": [0.234, -0.156, ...],
    "threshold": 0.7
}
Response: {
    "speaker_id": 1,
    "speaker_name": "Alice",
    "confidence": 0.85
}
```

### Configuration Parameters

| Parameter | Type | Default | Description |
|-----------|------|---------|-------------|
| `OPENSEARCH_HOST` | str | localhost | OpenSearch hostname |
| `OPENSEARCH_PORT` | str | 9200 | OpenSearch port |
| `OPENSEARCH_USER` | str | admin | OpenSearch username |
| `OPENSEARCH_PASSWORD` | str | admin | OpenSearch password |
| `OPENSEARCH_VERIFY_CERTS` | bool | False | Verify SSL certificates |
| `OPENSEARCH_NEURAL_SEARCH_ENABLED` | bool | True | Enable neural search |
| `OPENSEARCH_NEURAL_MODEL` | str | all-MiniLM-L6-v2 | Default embedding model |
| `OPENSEARCH_TRANSCRIPT_INDEX` | str | transcripts | Transcript index name |
| `OPENSEARCH_SPEAKER_INDEX` | str | speakers | Speaker index name (alias) |
| `SEARCH_RRF_RANK_CONSTANT` | int | 30 | RRF k constant (lower = more weight to top results) |
| `SEARCH_MAX_OVERFETCH` | int | 1000 | Max documents retrieved before RRF collapse |

### Environment Variables

```bash
# OpenSearch connection
export OPENSEARCH_HOST=opensearch
export OPENSEARCH_PORT=9200
export OPENSEARCH_USER=admin
export OPENSEARCH_PASSWORD=admin

# Neural search configuration
export OPENSEARCH_NEURAL_SEARCH_ENABLED=true
export OPENSEARCH_NEURAL_MODEL="huggingface/sentence-transformers/all-MiniLM-L6-v2"
export OPENSEARCH_NEURAL_PIPELINE="transcript-neural-ingest"

# Hybrid search tuning (v0.4.0)
export SEARCH_RRF_RANK_CONSTANT=30       # RRF k constant (default: 30)
export SEARCH_MAX_OVERFETCH=1000         # Max docs before RRF collapse

# Model caching (for offline deployments)
export MODEL_CACHE_DIR=./models
```

### Service Methods Quick Reference

```python
from app.services.opensearch_service import (
    get_opensearch_client,
    ensure_indices_exist,
    index_transcript,
    search_transcripts,
    find_matching_speaker,
    add_speaker_embedding,
    bulk_add_speaker_embeddings,
    store_profile_embedding,
    find_matching_profiles,
)

from app.services.search.ml_model_service import (
    get_ml_model_service,
    OpenSearchMLModelService,
)

# Usage
client = get_opensearch_client()
service = get_ml_model_service()

# Ensure indices
ensure_indices_exist()

# Index content
index_transcript(
    file_id=1,
    file_uuid="uuid",
    user_id=42,
    transcript_text="...",
    speakers=["Alice"],
    title="Meeting"
)

# Search
results = search_transcripts(
    query="query text",
    user_id=42,
    use_semantic=True
)

# Manage models
model_id = service.ensure_model_deployed("huggingface/sentence-transformers/all-MiniLM-L6-v2")
service.create_ingest_pipeline("transcript-neural-ingest", model_id)
status = service.get_model_status(model_id)
```

---

## Summary

OpenSearch ML Commons integration provides a powerful, production-grade search platform with:

- **Server-side embeddings** for efficient neural search
- **Six embedding models** supporting English and 50+ languages
- **Hybrid search** combining BM25 and vector search with RRF
- **Automatic model deployment** and lifecycle management
- **Offline support** for air-gapped deployments
- **High performance** with batching and caching
- **Comprehensive API** for all search and embedding operations

For more information, see the official documentation:
- [OpenSearch ML Commons](https://opensearch.org/docs/latest/ml-commons-plugin/index/)
- [Sentence Transformers](https://www.sbert.net/)
- [OpenSearch Vector Search](https://opensearch.org/docs/latest/search-plugins/search-relevance/vector-search/)
