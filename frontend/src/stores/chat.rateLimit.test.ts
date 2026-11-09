/** #788: a rate_limited error with Retry-After sets a deadline the UI counts down to. */

import { describe, it, expect, vi, beforeEach } from 'vitest';
import { get } from 'svelte/store';

vi.mock('$lib/api/chatStream', async () =>
  (await import('../test-mocks/chatStreamHarness')).chatStreamMock()
);
vi.mock('$lib/api/chatApi', async () =>
  (await import('../test-mocks/chatStreamHarness')).chatApiMock()
);

import * as chatApi from '$lib/api/chatApi';
import { chatStore } from './chat';
import { START, createChatHarness } from '../test-mocks/chatStreamHarness';

const { stream, prepare } = createChatHarness(chatStore, chatApi);

beforeEach(prepare);

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
