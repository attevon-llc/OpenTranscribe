/**
 * RAG chat store (issue #52).
 *
 * Holds only renderable state. The AbortController and other stream bookkeeping live in a
 * `ChatSession` beside the store — putting them in the store would make every token tick
 * re-serialize objects nothing renders.
 *
 * The actions are three groups over one store and session: conversations
 * (`$lib/chat/conversationActions`), the composer (`composerActions`) and streaming
 * (`streamActions`, whose frame reducer is `streamReducer`). Exactly ONE stream may be in
 * flight at a time: the composer's send button becomes Stop while streaming, which is both the
 * ChatGPT convention and what keeps the optimistic-message reconciliation tractable.
 */

import { writable } from 'svelte/store';
import { emptyScope } from '$lib/types/chat';
import { initialState, type ChatSession, type ChatState } from '$lib/chat/chatState';
import { createConversationActions } from '$lib/chat/conversationActions';
import { createComposerActions, pinnedModel } from '$lib/chat/composerActions';
import { createStreamActions } from '$lib/chat/streamActions';

export type { ChatState } from '$lib/chat/chatState';
export { pinnedModel };

function createChatStore() {
  const store = writable<ChatState>({ ...initialState });
  const session: ChatSession = {
    controller: null,
    pendingContext: null,
    pendingProject: null,
    selfCreated: new Set<string>(),
  };
  const ctx = { store, session };

  return {
    subscribe: store.subscribe,
    ...createConversationActions(ctx),
    ...createComposerActions(ctx),
    ...createStreamActions(ctx),

    reset(): void {
      session.controller?.abort();
      session.controller = null;
      session.pendingContext = null;
      session.selfCreated.clear();
      store.set({ ...initialState, scope: emptyScope() });
    },
  };
}

export const chatStore = createChatStore();
