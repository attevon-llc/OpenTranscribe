/**
 * The chat composer's state: the retrieval scope, context toggle, per-conversation settings
 * and model (buffered as drafts until a conversation exists), and what the gallery hands over.
 */
import { get } from 'svelte/store';
import * as chatApi from '$lib/api/chatApi';
import type { ChatScope, ConversationSettings } from '$lib/types/chat';
import type { ChatState, ChatStoreContext } from './chatState';

/** The model a turn would use: the open conversation's pin, else the draft's (null = default). */
export function pinnedModel(state: ChatState): string | null {
  return state.activeConversation
    ? state.activeConversation.llm_config_uuid ?? null
    : state.draftLlmConfigUuid;
}

export function createComposerActions({ store, session }: ChatStoreContext) {
  const { update } = store;

  async function refreshEstimate(): Promise<void> {
    const state = get(store);
    try {
      // The pinned model's window, not the account default's (#1364).
      const estimate = await chatApi.estimateContext(state.scope, pinnedModel(state));
      update((s) => ({ ...s, contextEstimate: estimate }));
    } catch {
      update((s) => ({ ...s, contextEstimate: null }));
    }
  }

  return {
    refreshEstimate,

    /**
     * File the next lazily-created conversation into a project (#360).
     *
     * Held here rather than creating an empty row on "new chat in project":
     * the row is created on first send, so an abandoned visit leaves no
     * conversation behind — the same reason `newConversation` creates nothing.
     */
    setPendingProject(uuid: string | null): void {
      session.pendingProject = uuid;
    },

    setScope(scope: ChatScope): void {
      update((s) => ({ ...s, scope }));
    },

    async persistScope(scope: ChatScope): Promise<void> {
      update((s) => ({ ...s, scope }));
      const uuid = get(store).activeConversationId;
      if (uuid) await chatApi.updateConversation(uuid, { scope });
    },

    async setUseContext(useContext: boolean): Promise<void> {
      update((s) => ({ ...s, useContext }));
      const uuid = get(store).activeConversationId;
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
      const uuid = get(store).activeConversationId;
      if (!uuid) {
        update((s) => ({ ...s, draftSettings: { ...s.draftSettings, ...patch } }));
        return;
      }
      const updated = await chatApi.updateConversation(uuid, { settings: patch });
      update((s) => ({ ...s, activeConversation: updated }));
    },

    /** Pin a model for this conversation, buffering before one exists. */
    async applyModel(llmConfigUuid: string | null): Promise<void> {
      const uuid = get(store).activeConversationId;
      if (!uuid) {
        update((s) => ({ ...s, draftLlmConfigUuid: llmConfigUuid }));
        await refreshEstimate();
        return;
      }
      const updated = await chatApi.updateConversation(uuid, {
        llm_config_uuid: llmConfigUuid,
      });
      update((s) => ({ ...s, activeConversation: updated }));
      await refreshEstimate();
    },

    /**
     * Hand a gallery selection to the chat page.
     *
     * Consumed once on mount rather than passed through the URL: a bulk
     * selection of a few hundred uuids would blow past URL length limits.
     */
    setPendingContext(fileUuids: string[]): void {
      session.pendingContext = fileUuids.length ? [...fileUuids] : null;
    },

    consumePendingContext(): string[] | null {
      const value = session.pendingContext;
      session.pendingContext = null;
      return value;
    },
  };
}
