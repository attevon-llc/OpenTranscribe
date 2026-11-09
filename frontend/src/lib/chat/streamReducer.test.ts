import { describe, expect, it } from 'vitest';
import { initialState, type ChatState } from './chatState';
import {
  markCancelled,
  markSendFailed,
  reduceStreamEvent,
  statusAfter,
  warningPatch,
} from './streamReducer';
import type { ChatMessage, ChatStreamEvent } from '$lib/types/chat';

const assistant = (over: Partial<ChatMessage> = {}): ChatMessage => ({
  uuid: 'a1',
  role: 'assistant',
  content: '',
  pending: true,
  status: 'streaming',
  ...over,
});

const streaming = (over: Partial<ChatMessage> = {}, state: Partial<ChatState> = {}): ChatState => ({
  ...initialState,
  streamingMessageId: 'a1',
  messages: [{ uuid: 'u1', role: 'user', content: 'hi', status: 'complete' }, assistant(over)],
  ...state,
});

const reduce = (s: ChatState, e: ChatStreamEvent) => reduceStreamEvent(s, e, 'local-a');

describe('statusAfter', () => {
  it.each([
    [{ type: 'status', stage: 'generating' }, 'thinking'],
    [{ type: 'status', stage: 'retrieving' }, 'retrieving'],
    [{ type: 'delta', text: 'x' }, 'streaming'],
    [{ type: 'done', finish_reason: 'stop' }, 'done'],
    [{ type: 'error', code: 'provider_error', message: 'm' }, 'error'],
    [{ type: 'sources', citations: [] }, null],
  ] as [ChatStreamEvent, string | null][])('%j moves the status to %s', (event, expected) => {
    expect(statusAfter(event)).toBe(expected);
  });
});

describe('warningPatch', () => {
  it('keeps the two counts on no_context and retrieval_failed under different keys', () => {
    const base = { type: 'warning', retrieved: 0, files_searched: 4 } as const;
    expect(warningPatch({ ...base, code: 'no_context' })).toEqual({
      no_context: true,
      retrieved: 0,
      files_searched: 4,
    });
    expect(warningPatch({ ...base, code: 'retrieval_failed' })).toEqual({
      retrieval_failed: true,
      retrieved: 0,
      files_searched: 4,
    });
  });

  it('omits a count the server did not send, and returns undefined for a code with no patch', () => {
    expect(warningPatch({ type: 'warning', code: 'context_dropped' })).toEqual({
      context_dropped: true,
    });
    expect(warningPatch({ type: 'warning', code: 'plan_failed' })).toBeUndefined();
  });
});

describe('reduceStreamEvent', () => {
  it('returns undefined, not the same state, for a frame that changes nothing', () => {
    const s = streaming();
    expect(reduce(s, { type: 'status', stage: 'generating' })).toBeUndefined();
    expect(reduce(s, { type: 'warning', code: 'plan_failed' })).toBeUndefined();
  });

  it('applies a frame only to the streaming message', () => {
    const next = reduce(streaming(), { type: 'delta', text: 'ok' })!;
    expect(next.messages.map((m) => m.content)).toEqual(['hi', 'ok']);
  });

  it('done leaves an errored turn errored and only clears pending', () => {
    const next = reduce(streaming({ status: 'error', error: 'boom' }), {
      type: 'done',
      finish_reason: 'stop',
    })!;
    expect(next.messages[1]).toMatchObject({ status: 'error', error: 'boom', pending: false });
  });

  it('done completes a normal turn and applies the server-chosen title everywhere', () => {
    const s = streaming(
      {},
      {
        activeConversationId: 'c1',
        conversations: [
          { uuid: 'c1', title: 'New chat' },
          { uuid: 'c2', title: 'Other' },
        ] as never,
        activeConversation: { uuid: 'c1', title: 'New chat' } as never,
      }
    );
    const next = reduce(s, { type: 'done', finish_reason: 'stop', title: 'Budget review' })!;
    expect(next.messages[1].status).toBe('complete');
    expect(next.conversations.map((c) => c.title)).toEqual(['Budget review', 'Other']);
    expect(next.activeConversation?.title).toBe('Budget review');
  });

  it('error records the code and a rate-limit deadline only for rate_limited', () => {
    const limited = reduce(streaming(), {
      type: 'error',
      code: 'rate_limited',
      message: 'slow down',
      retryAfter: 10,
    })!;
    expect(limited.error).toBe('rate_limited');
    expect(limited.rateLimitedUntil).toBeGreaterThan(Date.now());
    expect(limited.messages[1].msg_metadata).toMatchObject({ error_code: 'rate_limited' });

    const other = reduce(streaming(), { type: 'error', code: 'provider_error', message: 'x' })!;
    expect(other.rateLimitedUntil).toBeNull();
  });
});

describe('terminal marks', () => {
  it('markCancelled keeps the partial answer and clears pending', () => {
    const next = markCancelled(streaming({ content: 'partial' }));
    expect(next.messages[1]).toMatchObject({
      content: 'partial',
      status: 'cancelled',
      pending: false,
    });
  });

  it('markSendFailed fails the streaming message and flags the send error', () => {
    const next = markSendFailed(streaming());
    expect(next.error).toBe('send');
    expect(next.messages[1]).toMatchObject({ status: 'error', pending: false });
  });
});
