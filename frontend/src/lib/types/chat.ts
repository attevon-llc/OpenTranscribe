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

/** Whether the scope narrows nothing at all — no recordings AND no speakers. */
export function isScopeUnfiltered(scope: ChatScope | null | undefined): boolean {
  return isScopeEmpty(scope) && !(scope?.speakers?.length ?? 0);
}

/** One transcript excerpt an answer may reference as `[n]`. */
export interface ChatSource {
  id: number;
  file_uuid: string;
  title: string;
  chunk_index: number;
  start_time: number;
  end_time: number | null;
  speaker: string | null;
  snippet: string;
}

/** Diagnostics attached to an assistant message (ids/counts only). */
export interface ChatMessageMetadata {
  rewritten_query?: string;
  retrieved?: number;
  reranked?: number;
  cache_hit?: boolean;
  chunks_used?: number;
  files_searched?: number | 'all';
  timings_ms?: Record<string, number>;
  /**
   * Excerpts were retrieved but none fit the context window, so the answer is
   * NOT grounded in the user's recordings (issue #384). Persisted in
   * `msg_metadata`, and set live from the `warning` frame, so the notice
   * survives a reload rather than existing only for the streaming session.
   */
  context_dropped?: boolean;
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

type StreamStage = 'rewriting' | 'retrieving' | 'reranking' | 'generating';

/**
 * Non-fatal conditions the server surfaces mid-turn.
 *
 * `context_dropped`: retrieval found excerpts but the prompt budget left room
 * for none of them, so the answer is ungrounded. Reported rather than absorbed —
 * an answer that reads as sourced when it is not is the failure this exists to
 * prevent.
 *
 * `unsupported_language`: the context included recordings in a language the
 * English-only RAG stack cannot rank or read. Same principle: the answer looks
 * complete while a recording was invisible to it.
 *
 * ⚠️ A code missing from this union is silently discarded by `stores/chat.ts`,
 * so the server can emit a warning nobody ever sees. Widen both together.
 */
export type ChatWarningCode = 'context_dropped' | 'unsupported_language';

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
      /** `context_dropped` only: how many excerpts were retrieved but dropped. */
      retrieved?: number;
      /** `unsupported_language` only: the languages seen and how many files. */
      context_languages?: ChatMessageMetadata['context_languages'];
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
  | { type: 'done'; finish_reason: string; title?: string }
  | { type: 'error'; code: ChatErrorCode; message: string };

/** Where the composer/thread is in the send lifecycle. */
export type StreamStatus =
  | 'idle'
  | 'submitting'
  | 'retrieving'
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
