"""OpenSearch, search and RAG settings, with the validators that guard them.

Split out of ``app.core.config`` (v0.7.0 L07) as one cohesive concern, following the
``settings_*`` mixin pattern. ``Settings`` inherits these fields, so ``settings.<NAME>`` and
the environment variable names are unchanged. The env source, ``env_ignore_empty`` and
``extra="ignore"`` come from ``Settings``' own ``model_config``; this class is never
instantiated on its own.
"""

import os
from typing import ClassVar

from pydantic import ValidationInfo
from pydantic import field_validator
from pydantic_settings import BaseSettings

from app.core.chat_defaults import DEFAULT_CHAT_RERANKER_TORCH_THREADS
from app.core.env import env_bool
from app.utils.cpu_budget import parse_thread_count_setting


class SearchSettings(BaseSettings):
    # OpenSearch settings
    OPENSEARCH_HOST: str = os.getenv("OPENSEARCH_HOST", "localhost")
    OPENSEARCH_PORT: str = os.getenv("OPENSEARCH_PORT", "9200")
    # No "admin" fallback (issue #858): the security plugin ships disabled, so an
    # empty credential is exactly as inert as "admin" today, and if the plugin is
    # ever enabled with nothing configured, an empty credential fails closed --
    # OpenSearch answers 401 -- instead of an unconfigured install silently
    # succeeding on the plugin's own well-known bootstrap superuser password.
    OPENSEARCH_USER: str = os.getenv("OPENSEARCH_USER", "")
    OPENSEARCH_PASSWORD: str = os.getenv("OPENSEARCH_PASSWORD", "")
    OPENSEARCH_USE_TLS: bool = env_bool("OPENSEARCH_USE_TLS", False)
    OPENSEARCH_VERIFY_CERTS: bool = env_bool("OPENSEARCH_VERIFY_CERTS", False)
    # How to authenticate to OpenSearch (issue #284 A1.13). "basic" (default) sends the
    # OPENSEARCH_USER/OPENSEARCH_PASSWORD pair the bundled container expects. "sigv4"
    # signs every request with AWS SigV4 from the IAM-role chain, which is the only
    # accepted auth on an Amazon OpenSearch Service domain locked to an IAM policy.
    OPENSEARCH_AUTH: str = os.getenv("OPENSEARCH_AUTH", "basic")  # basic | sigv4
    # Signing region and service for OPENSEARCH_AUTH=sigv4. "es" is a managed domain;
    # "aoss" is OpenSearch Serverless (a different signing service name).
    # Plain "" default; resolved to AWS_REGION in `validate_auth_settings` below when
    # empty — same self-referential-default bug as S3_REGION/BEDROCK_REGION.
    OPENSEARCH_AWS_REGION: str = os.getenv("OPENSEARCH_AWS_REGION", "")  # empty -> AWS_REGION
    OPENSEARCH_AWS_SERVICE: str = os.getenv("OPENSEARCH_AWS_SERVICE", "es")  # es | aoss
    OPENSEARCH_TRANSCRIPT_INDEX: str = "transcripts"
    OPENSEARCH_SPEAKER_INDEX: str = "speakers"
    OPENSEARCH_SUMMARY_INDEX: str = "transcript_summaries"

    # Search & RAG settings
    OPENSEARCH_CHUNKS_INDEX: str = "transcript_chunks"
    # Index topology, applied ONLY at index CREATION (deployment-tier plan, AWS profile).
    # Defaults are the historical single-node values and MUST NOT change here — a changed
    # default silently reshards every fresh install, and shards cannot be changed on a live
    # index without a full reindex into a new one. Replicas > 0 need more than one data node
    # to mean anything; on a single-node box (laptop/home-server) they leave every replica
    # shard permanently UNASSIGNED and the index status yellow. See
    # docs-site/docs/operations/deployment-configuration.md's AWS profile section.
    OPENSEARCH_CHUNKS_INDEX_SHARDS: int = 1
    OPENSEARCH_CHUNKS_INDEX_REPLICAS: int = 0

    @field_validator("OPENSEARCH_CHUNKS_INDEX_SHARDS", "OPENSEARCH_CHUNKS_INDEX_REPLICAS")
    @classmethod
    def _clamp_chunks_index_topology(cls, v: int, info: ValidationInfo) -> int:
        """Floor each field again, AFTER pydantic-settings has resolved it.

        The ``max(_int_env(...), N)`` in the field default above only runs once, at class
        BODY execution time, to compute the field's *default* — but ``Settings`` is a
        ``BaseSettings`` subclass, so pydantic-settings separately re-reads the matching env
        var at every ``Settings()`` construction and casts it straight to ``int``, bypassing
        that default (and its clamp) entirely whenever the var is actually set. A negative
        ``OPENSEARCH_CHUNKS_INDEX_SHARDS`` env var reached ``settings.OPENSEARCH_CHUNKS_INDEX_
        SHARDS`` unclamped until this validator was added — caught by
        ``tests/unit/test_index_topology.py::test_negative_values_are_clamped_to_the_topology_floor``
        failing against the ``max()``-only version of this field.
        """
        floor = 1 if info.field_name == "OPENSEARCH_CHUNKS_INDEX_SHARDS" else 0
        return max(v, floor)

    # Torch intra-op threads for the chat reranker's scoring call (issue #1270): "auto" or a
    # positive integer. Deployment-scoped, not a SystemSettings row, because torch latches the
    # count per thread, so a live change would only apply after a restart. See
    # app/services/chat/reranker_threads.py.
    CHAT_RERANKER_TORCH_THREADS: str = DEFAULT_CHAT_RERANKER_TORCH_THREADS

    @field_validator("CHAT_RERANKER_TORCH_THREADS", mode="after")
    @classmethod
    def _validate_reranker_torch_threads(cls, v: str) -> str:
        """Refuse a value that is neither ``auto`` nor a positive integer."""
        try:
            parse_thread_count_setting(v)
        except ValueError as exc:
            raise ValueError(f"CHAT_RERANKER_TORCH_THREADS={exc}") from exc
        return v.strip().lower()

    OPENSEARCH_SEARCH_PIPELINE: str = "transcript-hybrid-search"

    # Indexing writes (bulk / index) are slower than queries under load; the client's 10 s
    # default timed out repeatedly and left files out of search (issue #1182). This applies to
    # indexing write calls only, never to search/query calls.
    OPENSEARCH_INDEX_TIMEOUT_S: float = 60.0
    # Extra in-call attempts after a read timeout. Safe because every indexing write uses an
    # explicit document _id, so a repeat overwrites rather than duplicates.
    OPENSEARCH_INDEX_TIMEOUT_RETRIES: int = 2
    # index_transcript_search_task keeps retrying for this long (exponential backoff with
    # jitter, per-attempt delay capped) before the row is marked failed; the periodic sweep
    # then re-dispatches it.
    SEARCH_INDEX_RETRY_HORIZON_S: int = 6 * 3600
    SEARCH_INDEX_RETRY_BASE_DELAY_S: int = 30
    SEARCH_INDEX_RETRY_MAX_DELAY_S: int = 600
    # Sweep that re-dispatches completed files whose search indexing failed or never ran.
    SEARCH_INDEX_SWEEP_BATCH_SIZE: int = 25
    SEARCH_INDEX_SWEEP_COOLDOWN_S: int = 1800
    SEARCH_INDEX_SWEEP_LOOKBACK_HOURS: int = 72
    SEARCH_CHUNK_TARGET_WORDS: int = 200
    SEARCH_CHUNK_OVERLAP_WORDS: int = 40
    SEARCH_RRF_RANK_CONSTANT: int = 30
    SEARCH_RRF_WINDOW_SIZE: int = 500
    # Hybrid fusion strategy (issue #363). "rrf" is the shipped default;
    # "normalization" selects OpenSearch's normalization-processor instead.
    # Env-only and deliberately NOT DB-backed, like SEARCH_RRF_RANK_CONSTANT above:
    # these are measurement knobs for the #363 A/B, not admin settings. A request
    # may override all four per call — see services/search/fusion.py.
    SEARCH_FUSION_STRATEGY: str = os.getenv("SEARCH_FUSION_STRATEGY", "rrf")
    SEARCH_NORMALIZATION_TECHNIQUE: str = os.getenv("SEARCH_NORMALIZATION_TECHNIQUE", "min_max")
    SEARCH_COMBINATION_TECHNIQUE: str = os.getenv("SEARCH_COMBINATION_TECHNIQUE", "arithmetic_mean")
    # Comma-separated per-leg weights, e.g. "0.7,0.3" (BM25, neural). Empty = equal.
    SEARCH_COMBINATION_WEIGHTS: str = os.getenv("SEARCH_COMBINATION_WEIGHTS", "")
    SEARCH_BULK_BATCH_SIZE: int = 100
    SEARCH_NEURAL_BATCH_SIZE: int = 5
    REINDEX_PARALLEL_WORKERS: int = 4
    SEARCH_HYBRID_MIN_SCORE: float = 0.005
    SEARCH_SEMANTIC_HIGH_CONFIDENCE: float = 0.010
    # Intra-semantic suppression: filter semantic-only results whose score falls
    # below this fraction of the semantic score range. 0.5 = keep top half.
    SEARCH_SEMANTIC_SUPPRESS_RATIO: float = 0.20

    # Max concurrent group searches for collapse inner_hits (OpenSearch default: 0 = sequential)
    SEARCH_COLLAPSE_MAX_CONCURRENT: int = 20
    # Maximum number of collapsed file groups to over-fetch for client-side sorting.
    # Higher values improve recall for large collections at the cost of memory.
    SEARCH_MAX_OVERFETCH: int = 1000
    # Chunk threshold above which bulk indexing temporarily disables OpenSearch
    # refresh (sets refresh_interval=-1) for the target index to avoid one
    # segment-refresh per bulk batch. The interval is restored afterwards so
    # normal search latency is unaffected. Tuned for 6+ hour transcripts.
    SEARCH_LARGE_TRANSCRIPT_CHUNKS: int = 500

    # OpenSearch Neural Search settings (ML Commons-based)
    # When enabled, embeddings are generated server-side by OpenSearch instead of Python
    OPENSEARCH_NEURAL_SEARCH_ENABLED: bool = env_bool("OPENSEARCH_NEURAL_SEARCH_ENABLED", True)
    # Default model to register/deploy (from OPENSEARCH_EMBEDDING_MODELS in constants.py)
    OPENSEARCH_NEURAL_MODEL: str = "huggingface/sentence-transformers/all-MiniLM-L6-v2"
    # Neural ingest pipeline name
    OPENSEARCH_NEURAL_PIPELINE: str = os.getenv(
        "OPENSEARCH_NEURAL_PIPELINE", "transcript-neural-ingest"
    )
    # Where the embedding model comes from (issue #284 A1.13).
    #   "local"   (default) — today's behaviour: we mutate ML Commons cluster settings,
    #             download OPENSEARCH_NEURAL_MODEL, and register/deploy it in the cluster.
    #   "managed" — the domain already hosts the model (a remote-model connector, or one
    #             an operator registered by hand). We touch no cluster settings and
    #             download nothing; OPENSEARCH_NEURAL_MODEL_ID names the model to use.
    # A managed OpenSearch domain rejects the "local" path outright: registering a model
    # from a file:// or arbitrary https:// URL needs cluster settings AWS does not expose.
    OPENSEARCH_EMBEDDING_MODE: str = os.getenv("OPENSEARCH_EMBEDDING_MODE", "local")
    # Pre-registered ML Commons model id, used when OPENSEARCH_EMBEDDING_MODE=managed.
    OPENSEARCH_NEURAL_MODEL_ID: str = os.getenv("OPENSEARCH_NEURAL_MODEL_ID", "")

    #: The only values OPENSEARCH_EMBEDDING_MODE may take. Derived from the single
    #: comparison site (services/search/neural_bootstrap.py::_managed_embedding_mode) —
    #: keep them together, or a new mode gets accepted here and ignored there.
    _OPENSEARCH_EMBEDDING_MODES: ClassVar[frozenset[str]] = frozenset({"local", "managed"})

    @field_validator("OPENSEARCH_EMBEDDING_MODE", mode="after")
    @classmethod
    def _validate_embedding_mode(cls, v: str) -> str:
        """Reject an unrecognized mode instead of silently falling through to "local".

        ``_managed_embedding_mode()`` compares this value with a single ``== "managed"``,
        so ANY other string — including a plausible typo like "app" — resolves to the
        "local" branch with no error. On a self-hosted cluster that still works (the local
        bootstrap registers/deploys the model itself); on a MANAGED domain the local path's
        cluster-settings mutation is rejected, and semantic search silently returns nothing
        while keyword search keeps working (issue #893).

        Normalizes the same way the comparison site does (strip + lower) so a
        trailing-space or differently-cased value is accepted rather than rejected on a
        technicality.
        """
        normalized = v.strip().lower()
        if normalized not in cls._OPENSEARCH_EMBEDDING_MODES:
            raise ValueError(
                f"OPENSEARCH_EMBEDDING_MODE={v!r} is not a valid mode. "
                f"Accepted values: {', '.join(sorted(cls._OPENSEARCH_EMBEDDING_MODES))}."
            )
        return normalized
