"""Coded defaults of the DB-backed RAG chat settings, and the reranker constants."""

# =============================================================================
# RAG chat (issue #52) — coded defaults for DB-backed SystemSettings.
# Every knob below is editable in the admin UI under its `chat.*` settings key
# with no restart; there are deliberately NO `.env` vars for chat.
# =============================================================================

# Retrieval shape. The candidate pool is what OpenSearch returns before
# reranking; final_chunks is what actually reaches the prompt. Capping chunks
# per file keeps one long recording from crowding out the rest of a multi-file
# selection — the whole point of chatting across transcripts.
DEFAULT_CHAT_RAG_CANDIDATE_POOL = 48  # chat.rag.candidate_pool
# 40/12 measured (#531, 2026-08-21): ~1.8-2x AMI-81 answer recall over 12/4 on two
# corpora, judge-corroborated, negative controls intact, +13% latency. With ~4 files
# in scope the per-file cap binds first, so raising final_chunks alone does nothing.
DEFAULT_CHAT_RAG_FINAL_CHUNKS = 40  # chat.rag.final_chunks
DEFAULT_CHAT_RAG_MAX_CHUNKS_PER_FILE = 12  # chat.rag.max_chunks_per_file

# Cross-encoder reranking (CPU-only, lazily loaded in the backend container).
DEFAULT_CHAT_RAG_RERANK_ENABLED = True  # chat.rag.rerank_enabled
DEFAULT_CHAT_RAG_RERANK_MAX_PAIRS = 50  # chat.rag.rerank_max_pairs
CHAT_RERANKER_MODEL = "cross-encoder/ms-marco-MiniLM-L-6-v2"
# Issue #1269: load CHAT_RERANKER_MODEL on a background thread at API startup so the
# first chat does not pay the ~10-15 s cold load. Read once per process at startup
# (SystemSettings row `chat.rag.rerank_warmup_enabled`, not an admin-panel flag); also
# skipped whenever chat.rag.rerank_enabled is off. See chat/reranker_warmup.py.
DEFAULT_CHAT_RAG_RERANK_WARMUP_ENABLED = True

# Issue #1270: the cross-encoder scores 50 pairs per turn, and torch's default intra-op
# thread count (physical cores: 24 on the 48-thread dev host) oversubscribes a busy box,
# so a warm rerank measured 7-16 s at the default against 3.4-5.5 s at 4-8 threads.
# `chat/reranker_threads.py` applies a cap to the scoring call only. The cap is a
# DEPLOYMENT setting (env `CHAT_RERANKER_TORCH_THREADS`, `auto` or a positive integer),
# not a SystemSettings row: torch latches the count once per thread, so a live admin
# value would not take effect until a restart. `auto` is
# min(CHAT_RERANKER_TORCH_THREADS_AUTO_MAX, max(1, effective_cpu_count() // 2)).
DEFAULT_CHAT_RERANKER_TORCH_THREADS = "auto"
CHAT_RERANKER_TORCH_THREADS_AUTO_MAX = 8  # value from scripts benchmark, see chat/CLAUDE.md
# Threads in the dedicated scoring pool: at most this many reranks run at once, each at the
# cap, instead of N concurrent turns each running at torch's default.
CHAT_RERANKER_SCORING_WORKERS = 2

# Multilingual reranker arm (issue #453/ML1) — a SELECTABLE second model, default OFF.
# `chat/reranker.py` skips reranking outright when the candidate pool is predominantly
# non-English, because CHAT_RERANKER_MODEL above is an English MS MARCO model and
# `rerank()` OVERWRITES `hit.score`, so letting it run destroys a correct retrieval
# order using a model that cannot read the text. This constant is the mechanism for a
# MEASURED alternative to that skip, not the decision to use it — the skip stays the
# shipped behaviour (this defaults False) until a sweep says otherwise.
#
# Licence, verified 2026-08-20 (huggingface.co model cards, not memory): bge-reranker-v2-m3
# is Apache-2.0. The only other shippable multilingual candidate is bge-reranker-base (MIT) —
# `jina-reranker-v2` is CC-BY-NC (non-commercial, unshippable in a published image) and the
# `gte`-family rerankers require `trust_remote_code=True` (arbitrary code execution from a
# downloaded model repo). Neither of those two is acceptable here.
DEFAULT_CHAT_RAG_MULTILINGUAL_RERANK_ENABLED = False  # chat.rag.multilingual_rerank_enabled
CHAT_RERANKER_MODEL_MULTILINGUAL = "BAAI/bge-reranker-v2-m3"  # Apache-2.0

# Conversational query rewriting: expands pronouns/references ("what about her?")
# into a standalone query before retrieval.
DEFAULT_CHAT_RAG_QUERY_REWRITE_ENABLED = True  # chat.rag.query_rewrite_enabled

# Retrieval caching. Tier 1 is an exact-query cache; tier 2 (opt-in) reuses
# results for semantically near-identical questions.
DEFAULT_CHAT_RAG_CACHE_TTL_SECONDS = 300  # chat.rag.cache_ttl_seconds
DEFAULT_CHAT_RAG_SEMANTIC_CACHE_ENABLED = False  # chat.rag.semantic_cache_enabled
DEFAULT_CHAT_RAG_SEMANTIC_CACHE_THRESHOLD = 0.97  # chat.rag.semantic_cache_threshold

# Conversation shape and abuse controls.
DEFAULT_CHAT_HISTORY_MAX_TURNS = 10  # chat.history_max_turns
DEFAULT_CHAT_MESSAGES_PER_HOUR = 120  # chat.limits.messages_per_hour
# Raised 2 -> 6 after a cap of 2 was measured starving ordinary use. A slot is
# held for `_ACTIVE_TTL_SECONDS` (300) when a stream dies without releasing —
# a cancelled generation, a closed tab — so at 2 a user who stops one answer and
# immediately asks two more is refused with "Too many chats streaming at once"
# until a slot ages out. The whole `-m chat` E2E suite, which is SERIAL and
# therefore never genuinely concurrent, failed a different random test on every
# run for exactly this reason; at 6 it is green and 2.5x faster.
#
# Still enforced for local providers, unlike the hourly quota next to it: that
# one is a SPEND control with nothing to control on your own GPU, while this
# bounds GPU contention, which is just as real for a self-hosted model. 6 is a
# starting point, not a policy — admins tune it live in Settings -> Chat & RAG
# (`chat-concurrent`), no restart and no env var, because it is a DB-backed
# SystemSettings row like every other knob in this block.
DEFAULT_CHAT_MAX_CONCURRENT_STREAMS = 6  # chat.limits.max_concurrent_streams
DEFAULT_CHAT_RETENTION_DAYS = 0  # chat.retention_days (0 = keep forever)

# the aggregation tier's speaker-facet/speaker-stats fixes. Both default
# OFF — they change what an existing question mechanism answers (facet scope)
# or add a new one (talk-time stats), and either is a measurement-gated rollout
# rather than a safe-by-construction default.
DEFAULT_CHAT_AGGREGATE_SPEAKER_FACET_CONTENT_SCOPE = (
    False  # chat.aggregate.speaker_facet_content_scope
)
DEFAULT_CHAT_AGGREGATE_SPEAKER_STATS_ENABLED = False  # chat.aggregate.speaker_stats_enabled

# #464: prefer each file's LLM summary over its digest in the bounded-scope map
# tier (`chat/mapreduce.scope_digest_hits`), when the summary is FRESH (its
# stored source_fingerprint matches the file's current file_facts row) — an
# absent or stale summary falls back to the digest unconditionally. Default OFF:
# on-by-default needs measured answer-quality evidence this flag does not yet
# have (a separate, later change).
DEFAULT_CHAT_MAP_TIER_SUMMARIES = False  # chat.rag.map_tier_summaries

# A provider that accepts the request but never emits a first token would
# otherwise hold the stream open until the read timeout.
DEFAULT_CHAT_FIRST_TOKEN_TIMEOUT_S = 90

# Per-user preferences (UserSetting keys `chat.system_prompt`,
# `chat.use_context_default`, `chat.default_search_mode`).
DEFAULT_CHAT_SYSTEM_PROMPT = ""
DEFAULT_CHAT_USE_CONTEXT = True
DEFAULT_CHAT_SEARCH_MODE = "hybrid"

# Resolved-scope ceiling: a selection resolving to more files than this is
# rejected (HTTP 400) rather than silently truncated.
CHAT_MAX_SCOPE_FILES = 500

# deterministic, Postgres-only speaker-mention resolution
# (`services/chat/speaker_resolver.py`) — matches a name typed in the question
# text against the caller's accessible roster and, on a unique match paired
# with a speaker-verb frame ("what did X say"), adds a PARALLEL speaker-scoped
# retrieval leg. Never replaces or narrows the main leg, and an explicit
# checkbox scope (`ChatScope.speakers`) is unaffected either way. Default OFF:
# this is a new, unmeasured retrieval shape.
DEFAULT_CHAT_SPEAKER_RESOLVER_ENABLED = False  # chat.speaker_resolver_enabled

# extends #464's map-tier-summaries pattern to the per-speaker map
# (`chat/mapreduce.scope_speaker_digest_hits`). When a fresh LLM summary exists
# for a file, prefer its `summary_data.speakers_analysis[]` entry (plus
# owner-matched action items) for the focus speaker over the per-sentence
# digest fallback. Default OFF for the same reason #464 is: on-by-default
# needs measured answer-quality evidence this flag does not yet have.
DEFAULT_CHAT_MAP_TIER_SPEAKER_SUMMARIES = False  # chat.rag.map_tier_speaker_summaries

# cross-meeting recurrence detection — "what keeps coming up across our
# meetings". Gates BOTH the router's recurrence lexicon (`router.classify`)
# and the `<recurrence>` evidence block (`recurrence_service.answer_recurrence`),
# so flag-off is byte-identical to before this feature existed on every layer,
# not just the shape. Default OFF: a new, unmeasured retrieval/synthesis shape
# whose masking subject also follows an unresolved-in-general policy question
# (issue #402) — see `services/chat/CLAUDE.md`.
DEFAULT_CHAT_RECURRENCE_ENABLED = False  # chat.recurrence_enabled

# the LLM query planner + parallel-leg fan-out
# (`services/chat/planner.py` + `services/chat/legs.py`). OFF by default: with
# no LLM configured (or the flag off) every turn routes exactly as it did
# before this module existed — rules-only routing, D6's "no LLM provider is
# still a first-class deployment" holds because nothing here is on the
# no-flag path. `needs_plan()` is pure and costs nothing to evaluate; only
# actually BUILDING a plan (an LLM call) is gated on this flag.
DEFAULT_CHAT_PLANNER_ENABLED = False  # chat.planner_enabled
# Ceiling on the process-wide leg executor (`legs.get_executor`). Sized once,
# at first use — a later change to this setting takes a process restart, the
# same trade every other lazy singleton in this package makes (see
# `reranker.py`).
DEFAULT_CHAT_PLANNER_MAX_PARALLEL_LEGS = 4  # chat.planner.max_parallel_legs
# a single bounded non-streaming call that reconciles merged evidence
# from a multi-leg fan-out into a `<synthesis>` block. OFF by default and
# INDEPENDENT of `chat.planner_enabled` — a deployment can run the fan-out
# without ever paying for the extra reconciliation call.
DEFAULT_CHAT_ENRICHMENT_ENABLED = False  # chat.enrichment_enabled

# GH #514: stream a per-stage query-execution trace to the chat client.
#
# ON by default, and the default is MEASURED rather than assumed. Against the
# mock LLM on an isolated stack, cache-warmed, 45 samples per arm:
#
#   median  573.3ms -> 576.3ms   (+3.0ms, +0.5%)
#   p95     659.3ms -> 688.9ms   (+29.6ms, +4.5%)
#
# The median cost sits inside this host's own run-to-run noise (the untraced
# arm's median ranged 544-573ms across four runs), so a typical turn pays
# nothing distinguishable. The p95 cost is real and small: ~15 extra SSE frames
# reach the socket before the first answer token.
#
# ⚠️ An EARLIER shape of this failed the same gate at +206ms p95 (+35%) because
# the drain loop polled every 50ms. If this number regresses, look there first —
# `_keepalive_until_done` must wait on events, never on an interval.
DEFAULT_CHAT_TRACE_ENABLED = True  # chat.trace_enabled

# Issue #523: read-time "small-to-big" context expansion. A short retrieved
# chunk (`services/chat/context_expansion.py`'s `SHORT_CHUNK_WORD_THRESHOLD`)
# is widened to its surrounding exchange BY TIMESTAMP before it reaches
# `redactor.mask_chunks` — masking still applies to every widened word (the
# widened time range is what the cached-span rebuild reads), and the growth
# is bounded so it comes out of the SAME excerpt budget without silently
# evicting other files' evidence (`MAX_EXPANSION_SEGMENTS`/
# `MAX_EXPANDED_WORDS`). Default ON as of 2026-09-21: measured (2026-08-20/21,
# re-verified post-#526 on a TS3005/ES2002a A/B) that expansion delivers a
# real, reproducible answer-quality gain ("same chunks, richer content") with
# coverage a wash, not a regression, and #526 (the citation/quote-fidelity
# blocker — an expanded chunk's citation pointed at a shorter, unexpanded
# indexed span) is CLOSED on both the backend (`ChunkHit.expanded` +
# `EXPANDED_SNIPPET_CHARS`) and the reader (`ChatSources.svelte`'s expander,
# #913). citation_resolution_rate/citation_validity_rate stayed a perfect 1.0
# in both arms of the re-verification. See issue #523's closing comment for
# the full numbers, including a harness measurement artifact (filed
# separately) that made raw quote_fidelity look worse than it is.
DEFAULT_CHAT_CONTEXT_EXPANSION_ENABLED = True  # chat.rag.context_expansion_enabled

# --- #532 synthesis-gap EXPERIMENT flags. -----------------------------------
# The measured defect: retrieval OFFERS 99% of a multi-file scope, the answer
# cites 75% — and the worst observed turn cited one excerpt for every claim
# while holding an overview of all 4 recordings. These three flags are the
# one-variable-at-a-time arms of that experiment, matching the published
# multi-document "dispersion"/position-bias literature. ⚠️ EXPERIMENT flags,
# not features: after measurement each is either promoted to default and the
# flag DELETED, or reverted and DELETED with its arm table on #532. Do not
# build on them.
DEFAULT_CHAT_OVERVIEW_CITABLE = False  # chat.rag.overview_citable
DEFAULT_CHAT_OVERVIEW_BLOCK_RULE = False  # chat.rag.overview_block_rule
DEFAULT_CHAT_OVERVIEW_AFTER_EXCERPTS = False  # chat.rag.overview_after_excerpts
