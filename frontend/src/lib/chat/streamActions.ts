/**
 * Sending, editing and regenerating a chat turn, and stopping one in flight. Exactly ONE
 * stream may be in flight at a time: the composer's send button becomes Stop while streaming,
 * which is both the ChatGPT convention and what keeps the optimistic-message reconciliation
 * tractable.
 */
import { get } from 'svelte/store';
import { resolve } from '$app/paths';
import { goto } from '$app/navigation';
import { generateId } from '$lib/utils/ids';
import * as chatApi from '$lib/api/chatApi';
import { canTransition } from '$lib/utils/chatStateMachine';
import { streamChatMessage, streamEditMessage, streamRegenerate } from '$lib/api/chatStream';
import type {
  ChatMessage,
  ChatStreamEvent,
  ConversationSummary,
  SearchMode,
  StreamStatus,
} from '$lib/types/chat';
import type { ChatStoreContext } from './chatState';
import { markCancelled, markSendFailed, reduceStreamEvent, statusAfter } from './streamReducer';

type StreamRun = (onEvent: (e: ChatStreamEvent) => void, signal: AbortSignal) => Promise<void>;

function isBusy(status: StreamStatus): boolean {
  return status === 'streaming' || status === 'submitting';
}

function streamingPlaceholder(uuid: string): ChatMessage {
  return { uuid, role: 'assistant', content: '', pending: true, status: 'streaming' };
}

function setStatusOn(store: ChatStoreContext['store'], next: StreamStatus): void {
  store.update((state) =>
    canTransition(state.streamStatus, next) ? { ...state, streamStatus: next } : state
  );
}

// Creates the server row for the first send in a blank composer and moves the URL onto it.
// Returns its uuid, or null when creation failed (the store is then already in its error state).
async function createConversationForSend({
  store,
  session,
}: ChatStoreContext): Promise<string | null> {
  const { update } = store;
  const state = get(store);
  try {
    const created = await chatApi.createConversation({
      scope: state.scope,
      // Carry anything chosen in Chat Controls before the row existed.
      settings: { ...state.draftSettings, use_context: state.useContext },
      llm_config_uuid: state.draftLlmConfigUuid,
      project_uuid: session.pendingProject,
    });
    session.pendingProject = null;
    session.selfCreated.add(created.uuid);
    update((s) => ({
      ...s,
      activeConversationId: created.uuid,
      activeConversation: created,
      draftSettings: {},
      draftLlmConfigUuid: null,
      conversations: [{ ...created, message_count: 0 } as ConversationSummary, ...s.conversations],
    }));
    await goto(resolve('/chat/[[conversationId]]', { conversationId: created.uuid }), {
      replace: true,
      reset: false,
    });
    return created.uuid;
  } catch {
    setStatusOn(store, 'error');
    update((s) => ({ ...s, error: 'send' }));
    return null;
  }
}

export function createStreamActions(ctx: ChatStoreContext) {
  const { store, session } = ctx;
  const { update } = store;
  const setStatus = (next: StreamStatus) => setStatusOn(store, next);

  /** Reduce one SSE frame into store state. Single place; keeps callers dumb. */
  function applyEvent(event: ChatStreamEvent, assistantLocalId: string): void {
    const status = statusAfter(event);
    if (status) setStatus(status);
    const next = reduceStreamEvent(get(store), event, assistantLocalId);
    if (next) store.set(next);
  }

  function appendOptimisticTurn(text: string): string {
    const assistantLocalId = generateId('local-assistant');
    update((s) => ({
      ...s,
      error: null,
      rateLimitedUntil: null,
      tokenUsage: null,
      messages: [
        ...s.messages,
        {
          uuid: generateId('local-user'),
          role: 'user',
          content: text,
          pending: true,
          status: 'complete',
        },
        streamingPlaceholder(assistantLocalId),
      ],
      streamingMessageId: assistantLocalId,
    }));
    return assistantLocalId;
  }

  async function runStream(assistantLocalId: string, run: StreamRun): Promise<void> {
    session.controller = new AbortController();
    try {
      await run((event) => applyEvent(event, assistantLocalId), session.controller.signal);
    } catch (err) {
      if ((err as Error)?.name === 'AbortError') {
        setStatus('aborted');
        update(markCancelled);
      } else {
        setStatus('error');
        update(markSendFailed);
      }
    } finally {
      session.controller = null;
      update((s) => ({ ...s, streamingMessageId: null }));
    }
  }

  return {
    async sendMessage(text: string, searchMode?: SearchMode): Promise<void> {
      const trimmed = text.trim();
      if (!trimmed) return;

      const state = get(store);
      if (isBusy(state.streamStatus)) return;

      setStatus('submitting');
      const assistantLocalId = appendOptimisticTurn(trimmed);

      const conversationUuid = state.activeConversationId ?? (await createConversationForSend(ctx));
      if (!conversationUuid) return;

      await runStream(assistantLocalId, (onEvent, signal) =>
        streamChatMessage(
          conversationUuid,
          { content: trimmed, search_mode: searchMode },
          onEvent,
          signal
        )
      );
    },

    /**
     * Rewrite an earlier question and re-answer from there.
     *
     * Everything after the edited turn is dropped locally (the server marks it
     * superseded) — a conversation is a chain, so answers that followed the old
     * wording are no longer about anything the user asked.
     */
    async editMessage(messageUuid: string, content: string): Promise<void> {
      const state = get(store);
      const trimmed = content.trim();
      if (!state.activeConversationId || !trimmed) return;
      if (isBusy(state.streamStatus)) return;

      const index = state.messages.findIndex((m) => m.uuid === messageUuid);
      const original = state.messages[index];
      if (!original) return;

      setStatus('submitting');
      const assistantLocalId = generateId('local-assistant');
      update((s) => ({
        ...s,
        error: null,
        tokenUsage: null,
        messages: [
          ...s.messages.slice(0, index),
          { ...original, content: trimmed, pending: true },
          streamingPlaceholder(assistantLocalId),
        ],
        streamingMessageId: assistantLocalId,
      }));

      await runStream(assistantLocalId, (onEvent, signal) =>
        streamEditMessage(state.activeConversationId!, messageUuid, trimmed, onEvent, signal)
      );
    },

    async regenerate(): Promise<void> {
      const state = get(store);
      if (!state.activeConversationId) return;
      if (isBusy(state.streamStatus)) return;

      // Drop the previous answer locally; the server marks it superseded.
      update((s) => {
        const lastAssistant = [...s.messages].reverse().find((m) => m.role === 'assistant');
        return {
          ...s,
          error: null,
          messages: lastAssistant
            ? s.messages.filter((m) => m.uuid !== lastAssistant.uuid)
            : s.messages,
        };
      });

      setStatus('submitting');
      const assistantLocalId = generateId('local-assistant');
      update((s) => ({
        ...s,
        messages: [...s.messages, streamingPlaceholder(assistantLocalId)],
        streamingMessageId: assistantLocalId,
      }));

      await runStream(assistantLocalId, (onEvent, signal) =>
        streamRegenerate(state.activeConversationId!, onEvent, signal)
      );
    },

    /** Stop generation: abort the fetch AND tell the server, in case the abort is lost. */
    stopGeneration(): void {
      const messageId = get(store).streamingMessageId;
      session.controller?.abort();
      if (messageId && !messageId.startsWith('local-')) {
        chatApi.cancelMessage(messageId).catch(() => undefined);
      }
    },
  };
}
