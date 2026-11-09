/**
 * The chat store's renderable state, its initial value, and the shared context the store's
 * action groups (`conversationActions`, `composerActions`, `streamActions`) work against.
 */
import type { Writable } from 'svelte/store';
import {
  emptyScope,
  type ChatMessage,
  type ChatScope,
  type ContextEstimate,
  type Conversation,
  type ConversationSettings,
  type ConversationSummary,
  type StreamStatus,
  type TokenUsage,
} from '$lib/types/chat';

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
  /** Epoch ms until which a 429's `Retry-After` says to wait; null when not rate-limited. */
  rateLimitedUntil: number | null;
}

export const initialState: ChatState = {
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
  rateLimitedUntil: null,
};

/**
 * Stream bookkeeping — deliberately outside the store: putting an AbortController in it would
 * make every token tick re-serialize objects nothing renders.
 */
export interface ChatSession {
  controller: AbortController | null;
  /** Files handed over by the gallery, consumed once on the chat page's mount. */
  pendingContext: string[] | null;
  /** Project the next lazily-created conversation joins (#360). */
  pendingProject: string | null;
  /**
   * Conversations this store created itself. The route syncs `conversationId` from the URL and
   * normally reloads on change, but when WE navigate after lazily creating a conversation, that
   * reload would wipe the optimistic messages and the in-flight stream's target — losing the
   * entire first answer.
   */
  selfCreated: Set<string>;
}

export interface ChatStoreContext {
  store: Writable<ChatState>;
  session: ChatSession;
}
