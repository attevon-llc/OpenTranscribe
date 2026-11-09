/**
 * The chat store's conversation list and lifecycle: paging and search, opening, creating a
 * blank composer, renaming, archiving, deleting and exporting.
 */
import { get } from 'svelte/store';
import { resolve } from '$app/paths';
import { goto } from '$app/navigation';
import * as chatApi from '$lib/api/chatApi';
import { emptyScope } from '$lib/types/chat';
import type { ChatStoreContext } from './chatState';

const CONVERSATIONS_PAGE_SIZE = 30;

export function createConversationActions({ store, session }: ChatStoreContext) {
  const { update } = store;

  async function loadConversations(reset = false): Promise<void> {
    const state = get(store);
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

  /** Reset to a blank composer without creating a server row yet. */
  function newConversation(): void {
    update((s) => ({
      ...s,
      activeConversationId: null,
      activeConversation: null,
      messages: [],
      streamStatus: 'idle',
      streamingMessageId: null,
      tokenUsage: null,
      error: null,
      scope: session.pendingContext
        ? { ...emptyScope(), file_uuids: session.pendingContext }
        : emptyScope(),
    }));
  }

  return {
    loadConversations,
    openConversation,
    newConversation,

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
      return session.selfCreated.delete(uuid);
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
      const wasActive = get(store).activeConversationId === uuid;
      update((s) => ({
        ...s,
        conversations: s.conversations.filter((c) => c.uuid !== uuid),
        conversationsTotal: Math.max(0, s.conversationsTotal - 1),
      }));
      if (wasActive) {
        newConversation();
        await goto(resolve('/chat/[[conversationId]]', {}), { replace: true });
      }
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
      const uuid = get(store).activeConversationId;
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
  };
}
