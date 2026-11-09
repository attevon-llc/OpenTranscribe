/**
 * Shared harness for the `chatStore` SSE-reducer tests.
 *
 * The store is driven the way production drives it, through `sendMessage()` with
 * `$lib/api/chatStream` and `$lib/api/chatApi` mocked. The mock factories live here so each
 * test file declares them in one line:
 *
 *   vi.mock('$lib/api/chatStream', async () => (await import('../test-mocks/chatStreamHarness')).chatStreamMock());
 *   vi.mock('$lib/api/chatApi', async () => (await import('../test-mocks/chatStreamHarness')).chatApiMock());
 *
 * This module must not import `./chat` statically: the mock factory imports it while `./chat`
 * is still loading, which would be a cycle.
 */

import { vi } from 'vitest';
import type { ChatStreamEvent } from '$lib/types/chat';

export const ASSISTANT_UUID = 'server-assistant-1';
export const USER_UUID = 'server-user-1';

export const START: ChatStreamEvent = {
  type: 'start',
  conversation_uuid: 'conv-1',
  user_message_uuid: USER_UUID,
  assistant_message_uuid: ASSISTANT_UUID,
};

/** What the mocked stream will do on the next turn; set per test via `stream()`. */
export const script: {
  frames: ChatStreamEvent[];
  error: Error | null;
  signal: AbortSignal | null;
} = { frames: [], error: null, signal: null };

export const streamChatMessage = vi.fn(
  async (
    _uuid: string,
    _payload: unknown,
    onEvent: (e: ChatStreamEvent) => void,
    signal: AbortSignal
  ) => {
    script.signal = signal;
    for (const frame of script.frames) {
      if (signal.aborted) break;
      onEvent(frame);
    }
    if (script.error) throw script.error;
  }
);

export function chatStreamMock() {
  return {
    streamChatMessage: (...args: Parameters<typeof streamChatMessage>) =>
      streamChatMessage(...args),
    streamEditMessage: vi.fn(),
    streamRegenerate: vi.fn(),
  };
}

export function chatApiMock() {
  return {
    createConversation: vi.fn(),
    updateConversation: vi.fn(),
    cancelMessage: vi.fn().mockResolvedValue(undefined),
    exportConversation: vi.fn(),
    listConversations: vi.fn(),
    getConversation: vi.fn(),
  };
}

interface HarnessStore {
  reset(): void;
  sendMessage(text: string): Promise<unknown>;
}
type HarnessApi = Pick<typeof import('$lib/api/chatApi'), 'createConversation' | 'cancelMessage'>;

/** Bind the harness to the store and the (mocked) api module under test. */
export function createChatHarness(chatStore: HarnessStore, chatApi: HarnessApi) {
  return {
    /** Run one turn through the reducer with the given frame script. */
    async stream(frames: ChatStreamEvent[], error: Error | null = null) {
      script.frames = frames;
      script.error = error;
      await chatStore.sendMessage('What did they decide?');
    },
    /** Call from `beforeEach`. The store creates the conversation on the first send. */
    prepare() {
      chatStore.reset();
      script.frames = [];
      script.error = null;
      script.signal = null;
      vi.clearAllMocks();
      vi.mocked(chatApi.createConversation).mockResolvedValue({
        uuid: 'conv-1',
        title: '',
        is_archived: false,
        message_count: 0,
        scope: { file_uuids: null, collection_uuids: null, tag_names: null },
      } as never);
      vi.mocked(chatApi.cancelMessage).mockResolvedValue(undefined as never);
    },
  };
}
