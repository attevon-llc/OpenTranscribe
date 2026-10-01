/** #788: a rate_limited error with Retry-After sets a deadline the UI counts down to. */

import { describe, it, expect, vi, beforeEach } from 'vitest';
import { get } from 'svelte/store';
import type { ChatStreamEvent } from '$lib/types/chat';

let scriptedFrames: ChatStreamEvent[] = [];

const streamChatMessage = vi.fn(
  async (
    _uuid: string,
    _payload: unknown,
    onEvent: (e: ChatStreamEvent) => void,
    _signal: AbortSignal
  ) => {
    for (const frame of scriptedFrames) {
      onEvent(frame);
    }
  }
);

vi.mock('$lib/api/chatStream', () => ({
  streamChatMessage: (...args: Parameters<typeof streamChatMessage>) => streamChatMessage(...args),
  streamEditMessage: vi.fn(),
  streamRegenerate: vi.fn(),
}));

vi.mock('$lib/api/chatApi', () => ({
  createConversation: vi.fn(),
  updateConversation: vi.fn(),
  cancelMessage: vi.fn().mockResolvedValue(undefined),
  exportConversation: vi.fn(),
  listConversations: vi.fn(),
  getConversation: vi.fn(),
}));

import * as chatApi from '$lib/api/chatApi';
import { chatStore } from './chat';

const ASSISTANT_UUID = 'server-assistant-1';
const USER_UUID = 'server-user-1';

const START: ChatStreamEvent = {
  type: 'start',
  conversation_uuid: 'conv-1',
  user_message_uuid: USER_UUID,
  assistant_message_uuid: ASSISTANT_UUID,
};

function assistant() {
  const message = get(chatStore).messages.find((m) => m.uuid === ASSISTANT_UUID);
  if (!message) throw new Error('assistant message not found');
  return message;
}

async function stream(frames: ChatStreamEvent[]) {
  scriptedFrames = frames;
  await chatStore.sendMessage('What did they decide?');
}

beforeEach(() => {
  chatStore.reset();
  scriptedFrames = [];
  vi.clearAllMocks();
  vi.mocked(chatApi.createConversation).mockResolvedValue({
    uuid: 'conv-1',
    title: '',
    is_archived: false,
    message_count: 0,
    scope: { file_uuids: null, collection_uuids: null, tag_names: null },
  } as never);
  vi.mocked(chatApi.cancelMessage).mockResolvedValue(undefined as never);
});

describe('chat store — rate limit deadline', () => {
  it("records a deadline from the error frame's retryAfter and keeps the conversation", async () => {
    const before = Date.now();
    await stream([START, { type: 'error', code: 'rate_limited', message: 'x', retryAfter: 30 }]);
    const state = get(chatStore);
    expect(state.error).toBe('rate_limited');
    expect(state.rateLimitedUntil).toBeGreaterThanOrEqual(before + 30_000);
    expect(state.rateLimitedUntil).toBeLessThanOrEqual(Date.now() + 30_000);
    expect(state.messages.some((m) => m.content === 'What did they decide?')).toBe(true);
  });

  it('has no deadline when the header was missing, or for other errors', async () => {
    await stream([START, { type: 'error', code: 'rate_limited', message: 'x' }]);
    expect(get(chatStore).rateLimitedUntil).toBeNull();
    chatStore.reset();
    await stream([START, { type: 'error', code: 'timeout', message: 'x', retryAfter: 9 }]);
    expect(get(chatStore).rateLimitedUntil).toBeNull();
  });
});
