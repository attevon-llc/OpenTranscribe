/**
 * RAG chat types (issue #52).
 *
 * These mirror the backend Pydantic schemas in `backend/app/schemas/chat.py`
 * exactly. The SSE event union mirrors the frame contract emitted by
 * `backend/app/services/chat/service.py` — event names are a frozen contract
 * shared by both sides, so changing one without the other breaks streaming.
 */

export type SearchMode = 'hybrid' | 'semantic' | 'keyword';

type MessageRole = 'user' | 'assistant';

type MessageStatus = 'streaming' | 'complete' | 'error' | 'cancelled' | 'superseded';

/**
 * Retrieval scope.
 *
 * Files, collections and tags choose WHICH RECORDINGS to search; `speakers` is a
 * separate axis that narrows to WHO WAS TALKING within them. Because transcript
 * chunks are speaker turns, a speaker filter is exact — "what did Dana say about
 * pricing" retrieves only Dana's words.
 */
export interface ChatScope {
  file_uuids: string[];
  collection_uuids: string[];
  tag_names: string[];
  speakers: string[];
}

export function emptyScope(): ChatScope {
  return { file_uuids: [], collection_uuids: [], tag_names: [], speakers: [] };
}

/** Whether the RECORDING scope is unset ("all transcripts"). */
export function isScopeEmpty(scope: ChatScope | null | undefined): boolean {
  if (!scope) return true;
  return (
    scope.file_uuids.length === 0 &&
    scope.collection_uuids.length === 0 &&
    scope.tag_names.length === 0
  );
}

/**
 * What a citation points at (#403 Stage 4, widened #464).
 *
 * `chunk` is somebody's words at a timestamp. `digest` is DERIVED text — an
 * extractive summary of a span of the same recording — and must never be
 * rendered as a quote: presenting it as speech attributes to a person words
 * nobody actually said. `summary` (#464) is LLM-generated prose ABOUT the
 * recording, not extracted from it — a labelled interpretation, never a
 * quote, and never attributed to a speaker, same as `digest` but a different
 * provenance the UI badges differently. Older messages predate the field and
 * carry nothing, which is why every read treats an absent value as `chunk`.
 *
 * `recurrence` (W2.5) is a GROUP of items judged to be the same thing,
 * recurring across MULTIPLE recordings — never one person's words, never
 * anchored to a single moment, and not even anchored to a single FILE the
 * way `summary` is. It carries `file_uuids` (every recording the group
 * spans) instead of relying on `file_uuid` alone.
 *
 * This mirrors `backend/app/schemas/chat.py`'s `Citation.kind`, widened
 * deliberately at the same time for the same reason (see that schema's
 * docstring): growing it kind-by-kind risks a persisted message losing an
 * already-shipped field the next time this union grows.
 */
export type ChatSourceKind = 'chunk' | 'digest' | 'summary' | 'recurrence';

/** One retrieved excerpt an answer may reference as `[n]`. */
export interface ChatSource {
  id: number;
  /** Absent on messages persisted before Stage 4; treat as `chunk`. */
  kind?: ChatSourceKind;
  file_uuid: string;
  title: string;
  chunk_index: number;
  /** Section number when `kind` is `digest` or `summary`, else null. */
  digest_section?: number | null;
  /**
   * `null`/absent for a kind with no natural single timestamp — a `summary`
   * citation describes the whole recording, and a `recurrence` citation
   * spans several. The absence is a first-class case on purpose: rendering a
   * missing timestamp as `0` would look like a working "jump to 0:00" link
   * that just happens to be wrong.
   */
  start_time: number | null;
  end_time: number | null;
  /** Always null for a digest or summary: neither is one person's words. */
  speaker: string | null;
  snippet: string;
  /**
   * `kind === 'chunk'` ONLY (#526). True when the backend widened this
   * citation's `start_time`/`end_time`/`snippet` to the chunk's surrounding
   * exchange before masking (`chat.context_expansion_enabled`) — the
   * `chunk_index` still names the ORIGINAL, unexpanded indexed chunk, so this
   * is what tells a reader the span shown is wider than that index entry.
   * Absent/`false` on every citation from before this field existed, which is
   * the correct read for them: none of them were ever expanded.
   */
  expanded?: boolean;
  /**
   * `kind === 'recurrence'` ONLY: every recording the recurring group spans.
   * `undefined`/`null` for every other kind. `file_uuid` still carries the
   * PRIMARY (first) recording, so a reader that only knows the single-file
   * contract still gets a valid uuid.
   */
  file_uuids?: string[] | null;
}

/** Diagnostics attached to an assistant message (ids/counts only). */
export interface ChatMessageMetadata {
  rewritten_query?: string;
  retrieved?: number;
  reranked?: number;
  cache_hit?: boolean;
  chunks_used?: number;
  files_searched?: number | 'all';
  /**
   * How many of the caller's EXPLICIT file picks (never collections/tags)
   * could not be resolved into scope — inaccessible, deleted, or quarantined.
   * Absent (never zero) when nothing was dropped. Most visible for an admin,
   * whose file picker offers every tenant recording while scope resolution
   * applies no admin bypass on any axis: picking 40 files that resolve to 3
   * would otherwise read as an unexplained `files_searched: 3` with no signal
   * that 37 were silently excluded.
   */
  scope_files_dropped?: number;
  timings_ms?: Record<string, number>;
  /**
   * Excerpts were retrieved but none fit the context window, so the answer is
   * NOT grounded in the user's recordings (issue #384). Persisted in
   * `msg_metadata`, and set live from the `warning` frame, so the notice
   * survives a reload rather than existing only for the streaming session.
   */
  context_dropped?: boolean;
  /**
   * NOTHING reached the prompt: retrieval matched nothing, the search backend
   * was unavailable and degraded to a context-free answer, or masking failed
   * closed on every chunk (issue #438). Distinct from `context_dropped`, where
   * excerpts existed and the budget rejected them — read `retrieved` beside
   * this to tell an empty search (`0`) from fail-closed masking (non-zero).
   */
  no_context?: boolean;
  /**
   * A specialization of the "nothing reached the prompt" case above: the
   * chunk-plane search itself raised or had no client, rather than genuinely
   * returning zero hits. Mutually exclusive with `no_context` — a turn sets
   * exactly one of the two, never both, distinguishing "search was down" from
   * "your library has nothing about this" (issue #438's open half).
   */
  retrieval_failed?: boolean;
  /**
   * The turn's context included recordings in a language RAG is not tuned for.
   * Transcription is multilingual; retrieval, reranking and prompting are
   * English-only, so a non-English recording is effectively invisible to the
   * question and the model answers from whatever English material remains.
   * Set live from the `warning` frame and persisted, like `context_dropped`.
   */
  unsupported_language?: boolean;
  /** Per-turn language diagnostics backing {@link unsupported_language}. */
  context_languages?: {
    languages?: string[];
    files?: number;
    unknown_files?: number;
    supported?: string[];
  };
  /**
   * Wave 2: which `mapreduce.py` reducer produced the `<overview>` block —
   * `'code'` (no-LLM, first class per D6) or `'llm-batch'`. Absent when no
   * overview was built for this turn.
   */
  map_source?: string;
  /**
   * Wave 2: how a speaker-filtered turn resolved the requested names against
   * the recordings in scope. Shape matches `mapreduce`/router diagnostics,
   * not a persisted structure of its own.
   */
  speaker_resolution?: {
    matched?: string[];
    ambiguous?: string[];
  };
  /** Wave 2: the query plan the router assembled for this turn, if any. */
  plan?: {
    steps?: string[];
  };
  /**
   * Wave 2: names of retrieval/aggregation "legs" that failed and were
   * skipped rather than failing the whole turn.
   */
  legs_failed?: string[];
  /**
   * #403 W2.6: per-leg wall time (ms) for a planner-driven parallel fan-out
   * (`legs.FanOutResult.timings_ms`), keyed by leg name (`"main"`,
   * `"subquestion-0"`, `"speaker"`, `"counted"`, `"recurrence"`, …). Absent
   * unless the fan-out ran this turn.
   */
  leg_timings_ms?: Record<string, number>;
  /** #403 W2.6: `Object.keys(leg_timings_ms).length`, named for direct display. */
  leg_count?: number;
  /**
   * Wave 2: bounded LLM calls a reducer spent on this turn (e.g.
   * `BatchReducer`'s per-batch condense calls in `mapreduce.py`). #403 W2.6
   * extends this to the planner/enrichment/rewrite-extension calls too.
   */
  llm_calls?: number;
  /**
   * Wave 2: the question's language did not match what the router
   * expected/could route with confidence. Set live from the `warning` frame
   * (`ChatWarningCode.router_language_unmatched`) and persisted, like
   * {@link unsupported_language}.
   */
  router_language_unmatched?: boolean;
}

export interface ChatMessage {
  uuid: string;
  role: MessageRole;
  content: string;
  /**
   * A provider's separately-streamed reasoning/"thinking" text, rendered in its
   * own collapsed-by-default block above `content` — never mixed into it. Absent
   * or empty for user messages and for any assistant reply whose provider never
   * streamed one.
   */
  reasoning_content?: string | null;
  citations?: ChatSource[] | null;
  msg_metadata?: ChatMessageMetadata | null;
  prompt_tokens?: number | null;
  completion_tokens?: number | null;
  total_tokens?: number | null;
  tokens_estimated?: boolean;
  provider?: string | null;
  model?: string | null;
  status?: MessageStatus;
  error?: string | null;
  created_at?: string | null;
  /** Client-only: set while a message is being streamed or has not been reconciled. */
  pending?: boolean;
  /**
   * Client-only reasoning-stream bookkeeping, all ephemeral (never sent to or
   * read back from the server on the same field names as the persisted one).
   * `reasoningStreaming` is true from the first `reasoning` frame until the
   * first `delta` frame (or `done`/`error`) ends the reasoning phase.
   * `reasoningStartedAt` is a `Date.now()` timestamp for a live elapsed-time
   * counter; `reasoningDurationMs` is frozen once the phase ends. Both are
   * undefined for messages loaded from history — there is no live timing to
   * replay, only the persisted text.
   */
  /**
   * GH #514. The query-execution trace for this turn, folded from `trace`
   * frames. CLIENT-ONLY and deliberately not persisted: a typical trace is
   * 1.5-2.5 KB against a whole message row of ~1.5 KB, so storing it would
   * roughly double every conversation-load payload for diagnostics shown one
   * turn at a time. A message loaded from history therefore has none, which is
   * exactly the "traces are not stored" state the panel renders.
   */
  trace?: TraceState;
  reasoningStreaming?: boolean;
  reasoningStartedAt?: number;
  reasoningDurationMs?: number;
}

export interface ConversationSettings {
  use_context?: boolean | null;
  system_prompt?: string | null;
  temperature?: number | null;
  /** Reply length ceiling. null inherits the provider config; the server clamps. */
  max_tokens?: number | null;
  /** Nucleus sampling. null omits it from the request entirely. */
  top_p?: number | null;
  search_mode?: SearchMode | null;
  /**
   * Whether the model reasons before answering. null inherits the model's own
   * behaviour; `false` is honoured ONLY where the server measured a working
   * off-switch for the model in play (see `reasoning_off_switch` on the LLM
   * configurations response). Never render a control for this without that
   * measurement — a toggle over a model that reasons anyway is a false claim.
   */
  reasoning?: boolean | null;
}

export interface ConversationSummary {
  uuid: string;
  title: string | null;
  is_archived: boolean;
  last_message_at: string | null;
  created_at: string | null;
  updated_at: string | null;
  message_count: number;
  /** null = ungrouped. The sidebar groups on this (#360). */
  project_uuid?: string | null;
}

export interface Conversation extends ConversationSummary {
  scope: ChatScope;
  settings: ConversationSettings;
  llm_config_uuid: string | null;
  /** Resolved value: per-conversation override, else the user's default. */
  use_context: boolean;
}

export interface ConversationList {
  conversations: ConversationSummary[];
  total: number;
  limit: number;
  offset: number;
}

export interface MessageList {
  messages: ChatMessage[];
  total: number;
  limit: number;
  offset: number;
}

export interface SendMessageRequest {
  content: string;
  search_mode?: SearchMode;
}

export interface ChatUserSettings {
  system_prompt: string;
  use_context_default: boolean;
  default_search_mode: SearchMode;
  /** Preferred excerpt count. null inherits; the server clamps to the admin value. */
  final_chunks?: number | null;
  /** null inherits. Can only turn reranking OFF, never on when the admin has it off. */
  rerank_enabled?: boolean | null;
}

export interface ContextEstimate {
  file_count: number;
  estimated_tokens: number;
  context_window: number;
  pct: number;
  warning_level: 'ok' | 'warn' | 'over';
}

export interface TokenUsage {
  prompt_tokens: number;
  completion_tokens: number;
  total_tokens: number;
  estimated: boolean;
}

export interface ChatAdminSettings {
  candidate_pool: number;
  final_chunks: number;
  max_chunks_per_file: number;
  rerank_enabled: boolean;
  rerank_max_pairs: number;
  query_rewrite_enabled: boolean;
  cache_ttl_seconds: number;
  semantic_cache_enabled: boolean;
  semantic_cache_threshold: number;
  history_max_turns: number;
  messages_per_hour: number;
  max_concurrent_streams: number;
  retention_days: number;
}

// --- SSE frame contract (must match services/chat/service.py) ----------------

// #403 W2.6: 'planning' is a turn-1 (no-history) stage for a question that
// will trigger a standalone LLM query-planner call before retrieval starts —
// see `services/chat/service.py`'s `stream_reply`. Only reachable when
// `chat.planner_enabled` is on; every other deployment never emits it.
type StreamStage = 'rewriting' | 'retrieving' | 'reranking' | 'planning' | 'generating';

/**
 * Non-fatal conditions the server surfaces mid-turn.
 *
 * `context_dropped`: retrieval found excerpts but the prompt budget left room
 * for none of them, so the answer is ungrounded. Reported rather than absorbed —
 * an answer that reads as sourced when it is not is the failure this exists to
 * prevent.
 *
 * `no_context`: nothing reached the prompt at all, and the search itself was
 * not the cause — retrieval genuinely matched nothing, or masking dropped
 * every chunk it found. Distinguished from `context_dropped` by `retrieved`
 * being non-zero for the masking case; distinguished from `retrieval_failed`
 * (below) by the search having actually run.
 *
 * `retrieval_failed`: nothing reached the prompt because the chunk-plane
 * search itself raised or had no client (issue #438's open half, closed) —
 * distinct from `no_context`, which means the search ran and genuinely found
 * nothing. `context_dropped` / `no_context` / `retrieval_failed` are mutually
 * exclusive branches of one server-side `if`/`elif`.
 *
 * `unsupported_language`: the context included recordings in a language the
 * English-only RAG stack cannot rank or read. Same principle: the answer looks
 * complete while a recording was invisible to it.
 *
 * `ambiguous_speaker` / `recurrence_unavailable` / `plan_failed` /
 * `router_language_unmatched`: Wave 2 additions mirroring
 * `backend/app/schemas/chat.py`'s `ChatWarningCode` — no backend emitter uses
 * them yet, but the code must exist here before one can, or the first turn
 * that emits one is silently dropped.
 *
 * ⚠️ A code missing from this union is silently discarded by `stores/chat.ts`,
 * so the server can emit a warning nobody ever sees. Widen both together.
 */
export type ChatWarningCode =
  | 'context_dropped'
  | 'no_context'
  | 'retrieval_failed'
  | 'unsupported_language'
  | 'ambiguous_speaker'
  | 'recurrence_unavailable'
  | 'plan_failed'
  | 'router_language_unmatched';

/**
 * GH #514 — the query-execution trace vocabulary, mirroring
 * `backend/app/services/chat/trace.py`'s `QueryStage` exactly. A value missing
 * from this union is a stage the SPA cannot place, the same trap
 * `ChatWarningCode` above documents.
 *
 * NOTE retrieval runs a SINGLE chunk-plane query rather than several legs, so a
 * node for a plane that never ran would misreport what actually happened.
 */
// `TraceState` is owned by `$lib/chat/traceTree`, which owns the fold logic.
// A type-only import is erased at compile time, so the mutual reference between
// these two modules is not a runtime cycle — and one definition cannot drift
// from a copy.
import type { TraceState } from '$lib/chat/traceTree';
export type { TraceState };

export type TraceStage =
  | 'submitted'
  | 'validated'
  | 'parsed_names'
  | 'rewritten'
  | 'cache_lookup'
  | 'planned'
  | 'fanned_relational'
  | 'fanned_vector'
  | 'found'
  | 'reranked'
  | 'sampled'
  | 'expanded'
  | 'filtered'
  | 'budgeted'
  | 'reviewed'
  | 'presented';

/**
 * How a stage ended. `empty` and `skipped` are the pair this whole feature
 * exists to separate — "we looked and found nothing" versus "we never looked" —
 * so they must never be collapsed or rendered alike.
 */
export type TraceOutcome = 'ok' | 'empty' | 'skipped' | 'cached' | 'declined' | 'failed';

/**
 * Mirrors `trace.py`'s `SAFE_DETAIL_KEYS`. Deliberately NON-identifying: no file
 * title, speaker name, uuid or query text ever reaches a trace node, so no
 * rendering mistake here can leak one.
 */
export interface TraceDetail {
  plane?: 'chunk' | 'digest';
  source?: 'postgres' | 'opensearch' | 'cache' | 'llm';
  count?: number;
  kept?: number;
  dropped?: number;
  leg?: string;
  legs?: number;
  reason?: string;
  ms?: number;
  /** A CONFIGURED bound (max-per-file, budget chars) — never derived from content. */
  limit?: number;
}

export type ChatErrorCode =
  | 'llm_unconfigured'
  | 'quota_exceeded'
  | 'rate_limited'
  | 'provider_error'
  | 'timeout'
  | 'cancelled';

export type ChatStreamEvent =
  | {
      type: 'start';
      conversation_uuid: string;
      user_message_uuid: string;
      assistant_message_uuid: string;
    }
  | { type: 'status'; stage: StreamStage }
  | { type: 'sources'; citations: ChatSource[] }
  | {
      type: 'warning';
      code: ChatWarningCode;
      /**
       * `context_dropped`: how many excerpts were retrieved but dropped.
       * `no_context`: how many were retrieved at all — `0` is an empty search,
       * non-zero is masking having failed closed on every one.
       * `retrieval_failed`: always `0` — the search never completed.
       */
      retrieved?: number;
      /** `no_context` / `retrieval_failed` only: `'all'` for an unscoped turn, else a count. */
      files_searched?: number | 'all';
      /** `unsupported_language` only: the languages seen and how many files. */
      context_languages?: ChatMessageMetadata['context_languages'];
    }
  /**
   * GH #514. One node's state at one moment. A leg reports itself TWICE under
   * one `node_id` — a `fanned_*` when it starts and a `found` when it finishes
   * — and the client folds those into a single node that advances, never two
   * rows. `seq` is a delivery stamp; a gap means the recorder hit its cap.
   */
  | {
      type: 'trace';
      seq: number;
      stage: TraceStage;
      outcome: TraceOutcome;
      parent: string | null;
      node_id: string | null;
      detail: TraceDetail;
    }
  | { type: 'delta'; text: string }
  /**
   * A chunk of the model's separately-streamed reasoning/"thinking" text.
   * Shaped identically to `delta` — same `{ text }` payload — but rendered in
   * the UI's own collapsed-by-default block instead of the answer bubble.
   */
  | { type: 'reasoning'; text: string }
  | {
      type: 'usage';
      prompt_tokens: number;
      completion_tokens: number;
      total_tokens: number;
      estimated: boolean;
    }
  | {
      type: 'done';
      finish_reason: string;
      title?: string;
      /**
       * GH #514. The recorder dropped events. Carried here rather than as a
       * stage because truncation describes the WHOLE trace, and inventing a
       * stage for it would make the vocabulary describe the transport.
       */
      trace_truncated?: boolean;
    }
  | { type: 'error'; code: ChatErrorCode; message: string };

/** Where the composer/thread is in the send lifecycle. */
export type StreamStatus =
  | 'idle'
  | 'submitting'
  | 'retrieving'
  // #403 W2.6: a turn-1 standalone planner call is in flight (`stage:
  // 'planning'`). `stores/chat.ts`'s `status` fold maps the SSE `stage` onto
  // this — see that file's `case 'status':` for the current mapping; a code
  // owner outside this lane still needs to add `event.stage === 'planning'`
  // there for the live indicator to actually reach this value, the same way
  // every other Wave-2 SSE addition in this codebase ships its type first.
  | 'planning'
  | 'thinking'
  | 'streaming'
  | 'done'
  | 'error'
  | 'aborted';

/** A workspace grouping conversations, with a pinned scope and prompt layer (#360). */
export interface ChatProject {
  uuid: string;
  name: string;
  description?: string | null;
  is_archived: boolean;
  conversation_count: number;
  created_at?: string | null;
  updated_at?: string | null;
}

export interface ChatProjectDetail extends ChatProject {
  system_prompt?: string | null;
  scope: ChatScope;
  llm_config_uuid?: string | null;
  /** True when the project pins recordings, so new chats inherit a scope. */
  has_scope: boolean;
}

export interface ChatProjectList {
  projects: ChatProject[];
  total: number;
}
