/**
 * RAG chat store (issue #52).
 *
 * Holds only renderable state. The AbortController and other stream bookkeeping
 * live in module closures — putting them in the store would make every token
 * tick re-serialize objects nothing renders.
 *
 * Exactly ONE stream may be in flight at a time: the composer's send button
 * becomes Stop while streaming, which is both the ChatGPT convention and what
 * keeps the optimistic-message reconciliation tractable.
 */

import { get, writable } from 'svelte/store';
import { goto } from '$app/navigation';

import * as chatApi from '$lib/api/chatApi';
import { canTransition } from '$lib/utils/chatStateMachine';
import { streamChatMessage, streamEditMessage, streamRegenerate } from '$lib/api/chatStream';
import {
  emptyScope,
  type ChatMessage,
  type ChatScope,
  type ChatSource,
  type ChatStreamEvent,
  type ContextEstimate,
  type Conversation,
  type ConversationSettings,
  type ConversationSummary,
  type SearchMode,
  type StreamStatus,
  type TokenUsage,
} from '$lib/types/chat';

const CONVERSATIONS_PAGE_SIZE = 30;

export interface ChatState {
  conversations: ConversationSummary[];
  conversationsTotal: number;
  conversationsOffset: number;
  conversationsQuery: string;
  conversationsLoading: boolean;
  /** Whether the sidebar is showing archived conversations. */
  showArchived: boolean;
  activeConversationId: string | null;
  activeConversation: Conversation | null;
  messages: ChatMessage[];
  messagesLoading: boolean;
  streamStatus: StreamStatus;
  streamingMessageId: string | null;
  scope: ChatScope;
  useContext: boolean;
  /** Settings chosen before a conversation exists; applied at creation. */
  draftSettings: ConversationSettings;
  /** Model chosen before a conversation exists; applied at creation. */
  draftLlmConfigUuid: string | null;
  contextEstimate: ContextEstimate | null;
  tokenUsage: TokenUsage | null;
  error: string | null;
}

const initialState: ChatState = {
  conversations: [],
  conversationsTotal: 0,
  conversationsOffset: 0,
  conversationsQuery: '',
  conversationsLoading: false,
  showArchived: false,
  activeConversationId: null,
  activeConversation: null,
  messages: [],
  messagesLoading: false,
  streamStatus: 'idle',
  streamingMessageId: null,
  scope: emptyScope(),
  useContext: true,
  draftSettings: {},
  draftLlmConfigUuid: null,
  contextEstimate: null,
  tokenUsage: null,
  error: null,
};

function createChatStore() {
  const { subscribe, set, update } = writable<ChatState>({ ...initialState });

  // Stream bookkeeping — deliberately outside the store.
  let controller: AbortController | null = null;
  let pendingContext: string[] | null = null;
  /** Project the next lazily-created conversation joins (#360). */
  let pendingProject: string | null = null;
  // Conversations this store created itself. The route syncs `conversationId`
  // from the URL and normally reloads on change, but when WE navigate after
  // lazily creating a conversation, that reload would wipe the optimistic
  // messages and the in-flight stream's target — losing the entire first answer.
  const selfCreated = new Set<string>();

  function setStatus(next: StreamStatus): void {
    update((state) =>
      canTransition(state.streamStatus, next) ? { ...state, streamStatus: next } : state
    );
  }

  async function loadConversations(reset = false): Promise<void> {
    const state = get({ subscribe });
    if (state.conversationsLoading) return;

    const offset = reset ? 0 : state.conversationsOffset;
    update((s) => ({ ...s, conversationsLoading: true }));
    try {
      const page = await chatApi.listConversations({
        limit: CONVERSATIONS_PAGE_SIZE,
        offset,
        q: state.conversationsQuery || undefined,
        archived: state.showArchived,
      });
      update((s) => ({
        ...s,
        conversations: reset ? page.conversations : [...s.conversations, ...page.conversations],
        conversationsTotal: page.total,
        conversationsOffset: offset + page.conversations.length,
        conversationsLoading: false,
      }));
    } catch {
      update((s) => ({ ...s, conversationsLoading: false }));
    }
  }

  async function openConversation(uuid: string): Promise<void> {
    update((s) => ({
      ...s,
      activeConversationId: uuid,
      messagesLoading: true,
      messages: [],
      error: null,
      streamStatus: 'idle',
    }));
    try {
      const [conversation, history] = await Promise.all([
        chatApi.getConversation(uuid),
        chatApi.listMessages(uuid, { limit: 200 }),
      ]);
      update((s) => ({
        ...s,
        activeConversation: conversation,
        // Superseded turns are history bookkeeping, not part of the thread.
        messages: history.messages.filter((m) => m.status !== 'superseded'),
        scope: conversation.scope,
        useContext: conversation.use_context,
        messagesLoading: false,
      }));
    } catch {
      update((s) => ({
        ...s,
        messagesLoading: false,
        error: 'conversationNotFound',
      }));
    }
  }

  /** Reduce one SSE frame into store state. Single place; keeps callers dumb. */
  function applyEvent(event: ChatStreamEvent, assistantLocalId: string): void {
    switch (event.type) {
      case 'start':
        update((s) => ({
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
        }));
        break;

      case 'status':
        setStatus(event.stage === 'generating' ? 'thinking' : 'retrieving');
        break;

      case 'sources':
        update((s) => ({
          ...s,
          messages: s.messages.map((m) =>
            m.uuid === s.streamingMessageId ? { ...m, citations: event.citations } : m
          ),
        }));
        break;

      case 'delta':
        setStatus('streaming');
        update((s) => ({
          ...s,
          messages: s.messages.map((m) =>
            m.uuid === s.streamingMessageId ? { ...m, content: m.content + event.text } : m
          ),
        }));
        break;

      case 'usage':
        update((s) => ({
          ...s,
          tokenUsage: {
            prompt_tokens: event.prompt_tokens,
            completion_tokens: event.completion_tokens,
            total_tokens: event.total_tokens,
            estimated: event.estimated,
          },
          messages: s.messages.map((m) =>
            m.uuid === s.streamingMessageId
              ? {
                  ...m,
                  prompt_tokens: event.prompt_tokens,
                  completion_tokens: event.completion_tokens,
                  total_tokens: event.total_tokens,
                  tokens_estimated: event.estimated,
                }
              : m
          ),
        }));
        break;

      case 'done':
        setStatus('done');
        update((s) => ({
          ...s,
          messages: s.messages.map((m) => {
            if (m.uuid !== s.streamingMessageId) return m;
            // The server always sends `done`, even after an `error` frame. Only
            // clear the pending flag in that case — rewriting status to
            // 'complete' would erase the error text AND the Retry button,
            // leaving a blank bubble with no explanation.
            if (m.status === 'error' || m.status === 'cancelled') {
              return { ...m, pending: false };
            }
            return { ...m, pending: false, status: 'complete' };
          }),
          // The server names a conversation from its first question.
          conversations: event.title
            ? s.conversations.map((c) =>
                c.uuid === s.activeConversationId ? { ...c, title: event.title as string } : c
              )
            : s.conversations,
          activeConversation:
            event.title && s.activeConversation
              ? { ...s.activeConversation, title: event.title }
              : s.activeConversation,
        }));
        break;

      case 'error':
        setStatus('error');
        update((s) => ({
          ...s,
          error: event.code,
          messages: s.messages.map((m) =>
            m.uuid === s.streamingMessageId
              ? { ...m, status: 'error', error: event.message, pending: false }
              : m
          ),
        }));
        break;
    }
  }

  function appendOptimisticTurn(text: string): string {
    const assistantLocalId = `local-assistant-${crypto.randomUUID()}`;
    update((s) => ({
      ...s,
      error: null,
      tokenUsage: null,
      messages: [
        ...s.messages,
        {
          uuid: `local-user-${crypto.randomUUID()}`,
          role: 'user',
          content: text,
          pending: true,
          status: 'complete',
        },
        {
          uuid: assistantLocalId,
          role: 'assistant',
          content: '',
          pending: true,
          status: 'streaming',
        },
      ],
      streamingMessageId: assistantLocalId,
    }));
    return assistantLocalId;
  }

  async function runStream(
    assistantLocalId: string,
    run: (onEvent: (e: ChatStreamEvent) => void, signal: AbortSignal) => Promise<void>
  ): Promise<void> {
    controller = new AbortController();
    try {
      await run((event) => applyEvent(event, assistantLocalId), controller.signal);
    } catch (err) {
      if ((err as Error)?.name === 'AbortError') {
        // Stop was pressed — keep whatever streamed in; the server persists the
        // same partial content on its side.
        setStatus('aborted');
        update((s) => ({
          ...s,
          messages: s.messages.map((m) =>
            m.uuid === s.streamingMessageId ? { ...m, pending: false, status: 'cancelled' } : m
          ),
        }));
      } else {
        setStatus('error');
        update((s) => ({
          ...s,
          error: 'send',
          messages: s.messages.map((m) =>
            m.uuid === s.streamingMessageId ? { ...m, status: 'error', pending: false } : m
          ),
        }));
      }
    } finally {
      controller = null;
      update((s) => ({ ...s, streamingMessageId: null }));
    }
  }

  return {
    subscribe,

    loadConversations,
    openConversation,

    async searchConversations(query: string): Promise<void> {
      update((s) => ({ ...s, conversationsQuery: query, conversationsOffset: 0 }));
      await loadConversations(true);
    },

    /**
     * Whether this store just created the given conversation.
     *
     * Consumed once by the route so a later genuine navigation to the same
     * conversation still reloads it from the server.
     */
    consumeSelfCreated(uuid: string): boolean {
      return selfCreated.delete(uuid);
    },

    /**
     * File the next lazily-created conversation into a project (#360).
     *
     * Held here rather than creating an empty row on "new chat in project":
     * the row is created on first send, so an abandoned visit leaves no
     * conversation behind — the same reason `newConversation` creates nothing.
     */
    setPendingProject(uuid: string | null): void {
      pendingProject = uuid;
    },

    /** Reset to a blank composer without creating a server row yet. */
    newConversation(): void {
      update((s) => ({
        ...s,
        activeConversationId: null,
        activeConversation: null,
        messages: [],
        streamStatus: 'idle',
        streamingMessageId: null,
        tokenUsage: null,
        error: null,
        scope: pendingContext ? { ...emptyScope(), file_uuids: pendingContext } : emptyScope(),
      }));
    },

    async renameConversation(uuid: string, title: string): Promise<void> {
      const updated = await chatApi.updateConversation(uuid, { title });
      update((s) => ({
        ...s,
        conversations: s.conversations.map((c) => (c.uuid === uuid ? { ...c, title } : c)),
        activeConversation:
          s.activeConversationId === uuid
            ? { ...s.activeConversation!, ...updated }
            : s.activeConversation,
      }));
    },

    async deleteConversation(uuid: string): Promise<void> {
      await chatApi.deleteConversation(uuid);
      const wasActive = get({ subscribe }).activeConversationId === uuid;
      update((s) => ({
        ...s,
        conversations: s.conversations.filter((c) => c.uuid !== uuid),
        conversationsTotal: Math.max(0, s.conversationsTotal - 1),
      }));
      if (wasActive) {
        this.newConversation();
        await goto('/chat', { replaceState: true });
      }
    },

    setScope(scope: ChatScope): void {
      update((s) => ({ ...s, scope }));
    },

    async persistScope(scope: ChatScope): Promise<void> {
      update((s) => ({ ...s, scope }));
      const uuid = get({ subscribe }).activeConversationId;
      if (uuid) await chatApi.updateConversation(uuid, { scope });
    },

    async setUseContext(useContext: boolean): Promise<void> {
      update((s) => ({ ...s, useContext }));
      const uuid = get({ subscribe }).activeConversationId;
      if (uuid) {
        await chatApi.updateConversation(uuid, { settings: { use_context: useContext } });
      }
    },

    /**
     * Apply per-conversation settings, buffering them when no conversation
     * exists yet.
     *
     * Without the buffer, opening Chat Controls on a fresh /chat and setting a
     * system prompt, temperature or model silently did nothing — the handler
     * early-returned on a null id and the panel then re-read the unchanged
     * default, so the control appeared to snap back on its own.
     */
    async applySettings(patch: ConversationSettings): Promise<void> {
      const uuid = get({ subscribe }).activeConversationId;
      if (!uuid) {
        update((s) => ({ ...s, draftSettings: { ...s.draftSettings, ...patch } }));
        return;
      }
      const updated = await chatApi.updateConversation(uuid, { settings: patch });
      update((s) => ({ ...s, activeConversation: updated }));
    },

    /** Pin a model for this conversation, buffering before one exists. */
    async applyModel(llmConfigUuid: string | null): Promise<void> {
      const uuid = get({ subscribe }).activeConversationId;
      if (!uuid) {
        update((s) => ({ ...s, draftLlmConfigUuid: llmConfigUuid }));
        return;
      }
      const updated = await chatApi.updateConversation(uuid, {
        llm_config_uuid: llmConfigUuid,
      });
      update((s) => ({ ...s, activeConversation: updated }));
    },

    async refreshEstimate(): Promise<void> {
      const scope = get({ subscribe }).scope;
      try {
        const estimate = await chatApi.estimateContext(scope);
        update((s) => ({ ...s, contextEstimate: estimate }));
      } catch {
        update((s) => ({ ...s, contextEstimate: null }));
      }
    },

    /**
     * Hand a gallery selection to the chat page.
     *
     * Consumed once on mount rather than passed through the URL: a bulk
     * selection of a few hundred uuids would blow past URL length limits.
     */
    setPendingContext(fileUuids: string[]): void {
      pendingContext = fileUuids.length ? [...fileUuids] : null;
    },

    consumePendingContext(): string[] | null {
      const value = pendingContext;
      pendingContext = null;
      return value;
    },

    async sendMessage(text: string, searchMode?: SearchMode): Promise<void> {
      const trimmed = text.trim();
      if (!trimmed) return;

      const state = get({ subscribe });
      if (state.streamStatus === 'streaming' || state.streamStatus === 'submitting') return;

      setStatus('submitting');
      const assistantLocalId = appendOptimisticTurn(trimmed);

      let conversationUuid = state.activeConversationId;
      if (!conversationUuid) {
        try {
          const created = await chatApi.createConversation({
            scope: state.scope,
            // Carry anything chosen in Chat Controls before the row existed.
            settings: { ...state.draftSettings, use_context: state.useContext },
            llm_config_uuid: state.draftLlmConfigUuid,
            project_uuid: pendingProject,
          });
          pendingProject = null;
          conversationUuid = created.uuid;
          selfCreated.add(created.uuid);
          update((s) => ({
            ...s,
            activeConversationId: created.uuid,
            activeConversation: created,
            draftSettings: {},
            draftLlmConfigUuid: null,
            conversations: [
              { ...created, message_count: 0 } as ConversationSummary,
              ...s.conversations,
            ],
          }));
          await goto(`/chat/${created.uuid}`, { replaceState: true, noScroll: true });
        } catch {
          setStatus('error');
          update((s) => ({ ...s, error: 'send' }));
          return;
        }
      }

      await runStream(assistantLocalId, (onEvent, signal) =>
        streamChatMessage(
          conversationUuid!,
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
      const state = get({ subscribe });
      const trimmed = content.trim();
      if (!state.activeConversationId || !trimmed) return;
      if (state.streamStatus === 'streaming' || state.streamStatus === 'submitting') return;

      const index = state.messages.findIndex((m) => m.uuid === messageUuid);
      if (index === -1) return;

      setStatus('submitting');
      const assistantLocalId = `local-assistant-${crypto.randomUUID()}`;
      update((s) => ({
        ...s,
        error: null,
        tokenUsage: null,
        messages: [
          ...s.messages.slice(0, index),
          { ...s.messages[index], content: trimmed, pending: true },
          {
            uuid: assistantLocalId,
            role: 'assistant',
            content: '',
            pending: true,
            status: 'streaming',
          },
        ],
        streamingMessageId: assistantLocalId,
      }));

      await runStream(assistantLocalId, (onEvent, signal) =>
        streamEditMessage(state.activeConversationId!, messageUuid, trimmed, onEvent, signal)
      );
    },

    /**
     * Archive or restore a conversation.
     *
     * Either way it leaves the list currently on screen — archiving removes it
     * from the active list, restoring removes it from the archived list — so the
     * same optimistic removal is correct in both directions.
     */
    async setArchived(uuid: string, archived: boolean): Promise<void> {
      await chatApi.updateConversation(uuid, { is_archived: archived });
      update((s) => ({
        ...s,
        conversations: s.conversations.filter((c) => c.uuid !== uuid),
        conversationsTotal: Math.max(0, s.conversationsTotal - 1),
      }));
    },

    /** Switch the sidebar between active and archived conversations. */
    async toggleArchivedView(): Promise<void> {
      update((s) => ({ ...s, showArchived: !s.showArchived, conversationsOffset: 0 }));
      await loadConversations(true);
    },

    /** Download the active conversation as Markdown or JSON. */
    async exportConversation(format: 'markdown' | 'json' = 'markdown'): Promise<void> {
      const uuid = get({ subscribe }).activeConversationId;
      if (!uuid) return;
      const { blob, filename } = await chatApi.exportConversation(uuid, format);
      const url = URL.createObjectURL(blob);
      const anchorEl = document.createElement('a');
      anchorEl.href = url;
      anchorEl.download = filename;
      document.body.appendChild(anchorEl);
      anchorEl.click();
      anchorEl.remove();
      URL.revokeObjectURL(url);
    },

    async regenerate(): Promise<void> {
      const state = get({ subscribe });
      if (!state.activeConversationId) return;
      if (state.streamStatus === 'streaming' || state.streamStatus === 'submitting') return;

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
      const assistantLocalId = `local-assistant-${crypto.randomUUID()}`;
      update((s) => ({
        ...s,
        messages: [
          ...s.messages,
          {
            uuid: assistantLocalId,
            role: 'assistant',
            content: '',
            pending: true,
            status: 'streaming',
          },
        ],
        streamingMessageId: assistantLocalId,
      }));

      await runStream(assistantLocalId, (onEvent, signal) =>
        streamRegenerate(state.activeConversationId!, onEvent, signal)
      );
    },

    /** Stop generation: abort the fetch AND tell the server, in case the abort is lost. */
    stopGeneration(): void {
      const messageId = get({ subscribe }).streamingMessageId;
      controller?.abort();
      if (messageId && !messageId.startsWith('local-')) {
        chatApi.cancelMessage(messageId).catch(() => undefined);
      }
    },

    reset(): void {
      controller?.abort();
      controller = null;
      pendingContext = null;
      selfCreated.clear();
      set({ ...initialState, scope: emptyScope() });
    },
  };
}

export const chatStore = createChatStore();

/** Convenience for components that only need the sources of a message. */
export function messageSources(message: ChatMessage): ChatSource[] {
  return message.citations ?? [];
}
