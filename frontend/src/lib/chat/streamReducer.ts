/**
 * The frame -> state mapping for a chat stream. Pure: each reducer takes the store state and
 * one SSE frame and returns the next state (or `undefined` when the frame changes nothing the
 * store renders), so every frame type is testable without a store or a network.
 */
import type {
  ChatMessage,
  ChatMessageMetadata,
  ChatStreamEvent,
  ChatWarningCode,
  StreamStatus,
} from '$lib/types/chat';
import { foldTraceFrame, markTruncated } from './traceTree';
import type { ChatState } from './chatState';

type Frame<T extends ChatStreamEvent['type']> = Extract<ChatStreamEvent, { type: T }>;

/** The stream status a frame moves the store to, if any (applied before the state change). */
export function statusAfter(event: ChatStreamEvent): StreamStatus | null {
  switch (event.type) {
    case 'status':
      return event.stage === 'generating' ? 'thinking' : 'retrieving';
    case 'delta':
      return 'streaming';
    case 'done':
      return 'done';
    case 'error':
      return 'error';
    default:
      return null;
  }
}

// Applies `fn` to the message currently streaming, leaving every other message untouched.
function mapStreaming(s: ChatState, fn: (m: ChatMessage) => ChatMessage): ChatState {
  return {
    ...s,
    messages: s.messages.map((m) => (m.uuid === s.streamingMessageId ? fn(m) : m)),
  };
}

// The reasoning phase's elapsed time, frozen once the turn moves on from it so the collapsed
// header can show "Thought for Ns" instead of continuing to tick.
function reasoningDuration(m: ChatMessage): number | undefined {
  return m.reasoningStreaming && m.reasoningStartedAt !== undefined
    ? Date.now() - m.reasoningStartedAt
    : m.reasoningDurationMs;
}

function onStart(s: ChatState, event: Frame<'start'>, assistantLocalId: string): ChatState {
  return {
    ...s,
    streamingMessageId: event.assistant_message_uuid,
    messages: s.messages.map((m) => {
      if (m.uuid === assistantLocalId) {
        return { ...m, uuid: event.assistant_message_uuid };
      }
      if (m.pending && m.role === 'user') {
        return { ...m, uuid: event.user_message_uuid, pending: false };
      }
      return m;
    }),
  };
}

function onSources(s: ChatState, event: Frame<'sources'>): ChatState {
  return mapStreaming(s, (m) => ({ ...m, citations: event.citations }));
}

/**
 * The `msg_metadata` patch a warning frame folds into the streaming message, or undefined for
 * a code with no patch.
 *
 * Folded into msg_metadata rather than kept as separate stream state, so ChatMessage has ONE
 * render path: the same flag arrives on the persisted message when the thread is reloaded.
 *
 * Keyed by code rather than an if-chain: an unhandled code used to fall through to nothing, so
 * the server could emit a warning the SPA discarded in silence while still persisting it on the
 * message — the notice then appeared only after a reload. Adding a code to ChatWarningCode
 * without adding it here reintroduces exactly that.
 *
 * Typed as `Partial<Record<...>>` — NOT every ChatWarningCode needs an entry.
 * `ambiguous_speaker` / `recurrence_unavailable` / `plan_failed` have no backend emitter yet
 * and no corresponding msg_metadata shape decided yet either; indexing with one of them falls
 * through to `undefined` (no-op) rather than a compile error, until whichever lane wires the
 * emitter also adds the patch.
 */
export function warningPatch(event: Frame<'warning'>): Partial<ChatMessageMetadata> | undefined {
  const withRetrieved = event.retrieved === undefined ? {} : { retrieved: event.retrieved };
  const withFiles =
    event.files_searched === undefined ? {} : { files_searched: event.files_searched };
  const patchMap: Partial<Record<ChatWarningCode, Partial<ChatMessageMetadata>>> = {
    context_dropped: { context_dropped: true, ...withRetrieved },
    // `retrieved` and `files_searched` are folded in because on `no_context` the count is what
    // separates an empty search from masking having dropped every chunk, and neither is
    // otherwise on screen until the thread is reloaded.
    no_context: { no_context: true, ...withRetrieved, ...withFiles },
    // Same shape as `no_context` — the server sends the same two counts — but a DIFFERENT key,
    // so the two notices stay distinguishable in persisted metadata and ChatMessage can render
    // a different message for "search was down" than for "your library has nothing about this".
    retrieval_failed: { retrieval_failed: true, ...withRetrieved, ...withFiles },
    unsupported_language: {
      unsupported_language: true,
      context_languages: event.context_languages,
    },
    // Persisted like `unsupported_language` above so the notice survives a reload rather than
    // existing only for the streaming session.
    router_language_unmatched: { router_language_unmatched: true },
  };
  return patchMap[event.code];
}

function onWarning(s: ChatState, event: Frame<'warning'>): ChatState | undefined {
  const patch = warningPatch(event);
  if (!patch) return undefined;
  return mapStreaming(s, (m) => ({ ...m, msg_metadata: { ...(m.msg_metadata ?? {}), ...patch } }));
}

// GH #514. `foldTraceFrame` returns a NEW state and never mutates a node, which is what lets
// Svelte see the change: mutating in place under a spread-only update re-renders inconsistently.
function onTrace(s: ChatState, event: Frame<'trace'>): ChatState {
  return mapStreaming(s, (m) => ({ ...m, trace: foldTraceFrame(m.trace, event) }));
}

function onReasoning(s: ChatState, event: Frame<'reasoning'>): ChatState {
  return mapStreaming(s, (m) => ({
    ...m,
    reasoning_content: (m.reasoning_content ?? '') + event.text,
    reasoningStreaming: true,
    // Only set on the FIRST reasoning frame — later frames must not reset the clock, or a live
    // elapsed counter never advances.
    reasoningStartedAt: m.reasoningStartedAt ?? Date.now(),
  }));
}

// The first answer token ends the reasoning phase.
function onDelta(s: ChatState, event: Frame<'delta'>): ChatState {
  return mapStreaming(s, (m) => ({
    ...m,
    content: m.content + event.text,
    reasoningStreaming: false,
    reasoningDurationMs: reasoningDuration(m),
  }));
}

function onUsage(s: ChatState, event: Frame<'usage'>): ChatState {
  return {
    ...mapStreaming(s, (m) => ({
      ...m,
      prompt_tokens: event.prompt_tokens,
      completion_tokens: event.completion_tokens,
      total_tokens: event.total_tokens,
      tokens_estimated: event.estimated,
    })),
    tokenUsage: {
      prompt_tokens: event.prompt_tokens,
      completion_tokens: event.completion_tokens,
      total_tokens: event.total_tokens,
      estimated: event.estimated,
    },
  };
}

// The server always sends `done`, even after an `error` frame. Only clear the pending flag in
// that case — rewriting status to 'complete' would erase the error text AND the Retry button,
// leaving a blank bubble with no explanation. A turn that reasoned but never produced an answer
// got no `delta` frame to freeze the elapsed time, so that is done here too.
// GH #514: truncation is a property of the WHOLE trace, so it rides `done` rather than being
// invented as a stage. Widening QueryStage for it would make the vocabulary describe the
// transport instead of the pipeline.
function finishMessage(m: ChatMessage, event: Frame<'done'>): ChatMessage {
  const settled = {
    ...m,
    pending: false,
    reasoningStreaming: false,
    reasoningDurationMs: reasoningDuration(m),
    trace: event.trace_truncated ? markTruncated(m.trace) : m.trace,
  };
  return m.status === 'error' || m.status === 'cancelled'
    ? settled
    : { ...settled, status: 'complete' };
}

function onDone(s: ChatState, event: Frame<'done'>): ChatState {
  const done = mapStreaming(s, (m) => finishMessage(m, event));
  const title = event.title;
  if (!title) return done;
  // The server names a conversation from its first question.
  return {
    ...done,
    conversations: s.conversations.map((c) =>
      c.uuid === s.activeConversationId ? { ...c, title } : c
    ),
    activeConversation: s.activeConversation ? { ...s.activeConversation, title } : null,
  };
}

function onError(s: ChatState, event: Frame<'error'>): ChatState {
  return {
    ...mapStreaming(s, (m) => ({
      ...m,
      status: 'error',
      error: event.message,
      // Same place the server persists it, so live and reloaded turns render through one path
      // (issue #1049).
      msg_metadata: { ...(m.msg_metadata ?? {}), error_code: event.code },
      pending: false,
      reasoningStreaming: false,
      reasoningDurationMs: reasoningDuration(m),
    })),
    error: event.code,
    rateLimitedUntil:
      event.code === 'rate_limited' && event.retryAfter
        ? Date.now() + event.retryAfter * 1000
        : null,
  };
}

/** The next state after one SSE frame, or undefined when the frame changes no state. */
export function reduceStreamEvent(
  s: ChatState,
  event: ChatStreamEvent,
  assistantLocalId: string
): ChatState | undefined {
  switch (event.type) {
    case 'start':
      return onStart(s, event, assistantLocalId);
    case 'sources':
      return onSources(s, event);
    case 'warning':
      return onWarning(s, event);
    case 'trace':
      return onTrace(s, event);
    case 'reasoning':
      return onReasoning(s, event);
    case 'delta':
      return onDelta(s, event);
    case 'usage':
      return onUsage(s, event);
    case 'done':
      return onDone(s, event);
    case 'error':
      return onError(s, event);
    default:
      return undefined;
  }
}

/** Stop was pressed: keep whatever streamed in (the server persists the same partial content). */
export function markCancelled(s: ChatState): ChatState {
  return mapStreaming(s, (m) => ({
    ...m,
    pending: false,
    status: 'cancelled',
    reasoningStreaming: false,
    reasoningDurationMs: reasoningDuration(m),
  }));
}

/** The request itself failed (not an error frame): fail the streaming message. */
export function markSendFailed(s: ChatState): ChatState {
  return {
    ...mapStreaming(s, (m) => ({ ...m, status: 'error', pending: false })),
    error: 'send',
  };
}
