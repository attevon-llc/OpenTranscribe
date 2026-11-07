/** #1364: the context estimate measures against the model the conversation will use. */

import { describe, it, expect, vi, beforeEach } from 'vitest';

vi.mock('$lib/api/chatStream', () => ({
  streamChatMessage: vi.fn(),
  streamEditMessage: vi.fn(),
  streamRegenerate: vi.fn(),
}));
vi.mock('$lib/api/chatApi', () => ({
  createConversation: vi.fn(),
  updateConversation: vi.fn(),
  estimateContext: vi.fn(),
  listConversations: vi.fn(),
  getConversation: vi.fn(),
}));

import * as chatApi from '$lib/api/chatApi';
import { chatStore } from './chat';

const PINNED = '22222222-2222-2222-2222-222222222222';

beforeEach(async () => {
  chatStore.newConversation();
  vi.mocked(chatApi.estimateContext).mockResolvedValue({
    file_count: 0,
    estimated_tokens: 0,
    context_window: 200_000,
    context_window_source: 'model',
    context_window_unknown_reason: null,
    pct: 0,
    warning_level: 'ok',
  });
  await chatStore.applyModel(null); // the draft pin outlives newConversation()
  vi.mocked(chatApi.estimateContext).mockClear();
});

describe('chat context estimate', () => {
  it('re-estimates with the newly pinned model when a draft picks one', async () => {
    await chatStore.applyModel(PINNED);
    expect(chatApi.estimateContext).toHaveBeenLastCalledWith(expect.anything(), PINNED);
  });

  it('asks for the account default when nothing is pinned', async () => {
    await chatStore.refreshEstimate();
    expect(chatApi.estimateContext).toHaveBeenLastCalledWith(expect.anything(), null);
  });
});
