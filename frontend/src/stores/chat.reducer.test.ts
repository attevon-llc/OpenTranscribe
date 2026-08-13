/**
 * Tests for the SSE **reducer** in `$stores/chat` — the frame→state mapping.
 *
 * DEFECT THESE CATCH: `chatStream.test.ts` proves the PARSER (bytes → frames);
 * nothing proved the REDUCER (frames → renderable state), and the store sat at
 * 9/196 lines. All nine frame types converge on one `applyEvent` switch, so every
 * one of these is a silent failure:
 *   - a `delta` appended to the WRONG message (the reducer matches on
 *     `streamingMessageId`, which the `start` frame rewrites mid-flight);
 *   - an `error` frame that WIPES the partial answer instead of preserving it —
 *     the user loses everything the model already said, and the following `done`
 *     frame must not overwrite `status: 'error'` back to `complete`, which would
 *     erase both the error text and the Retry button;
 *   - `usage` not recorded, so the token counter silently reads zero;
 *   - `stopGeneration` not aborting, so Stop appears to do nothing;
 *   - reasoning elapsed time restarting on every frame, so "Thought for Ns" never
 *     advances.
 *
 * The reducer is driven the way production drives it: through `sendMessage()`
 * with `$lib/api/chatStream` mocked, so the frames pass through the real
 * optimistic-turn reconciliation instead of being handed to a private function.
 */

import { describe, it, expect, vi, beforeEach } from 'vitest';
import { get } from 'svelte/store';
import type { ChatStreamEvent } from '$lib/types/chat';

/** Frames the mocked stream will emit, set per test. */
let scriptedFrames: ChatStreamEvent[] = [];
/** Set to make the stream throw instead of completing. */
let streamError: Error | null = null;
/** The signal the store handed to the stream, for abort assertions. */
let lastSignal: AbortSignal | null = null;

const streamChatMessage = vi.fn(
  async (
    _uuid: string,
    _payload: unknown,
    onEvent: (e: ChatStreamEvent) => void,
    signal: AbortSignal
  ) => {
    lastSignal = signal;
    for (const frame of scriptedFrames) {
      if (signal.aborted) break;
      onEvent(frame);
    }
    if (streamError) throw streamError;
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

function state() {
  return get(chatStore);
}

function assistant() {
  const message = state().messages.find((m) => m.uuid === ASSISTANT_UUID);
  if (!message) throw new Error('assistant message not found');
  return message;
}

/** Run one turn through the reducer with the given frame script. */
async function stream(frames: ChatStreamEvent[], error: Error | null = null) {
  scriptedFrames = frames;
  streamError = error;
  await chatStore.sendMessage('What did they decide?');
}

beforeEach(() => {
  chatStore.reset();
  scriptedFrames = [];
  streamError = null;
  lastSignal = null;
  vi.clearAllMocks();
  // The store creates the conversation on the first send (the real production
  // path for a new chat). `goto` is the `$app/navigation` test stub.
  vi.mocked(chatApi.createConversation).mockResolvedValue({
    uuid: 'conv-1',
    title: '',
    is_archived: false,
    message_count: 0,
    scope: { file_uuids: null, collection_uuids: null, tag_names: null },
  } as never);
  vi.mocked(chatApi.cancelMessage).mockResolvedValue(undefined as never);
});

describe('chat reducer — start frame reconciliation', () => {
  it('swaps the optimistic local ids for the server uuids', async () => {
    await stream([
      START,
      { type: 'delta', text: 'They shipped.' },
      { type: 'done', finish_reason: 'stop' },
    ]);

    const ids = state().messages.map((m) => m.uuid);
    expect(ids).toEqual([USER_UUID, ASSISTANT_UUID]);
    // The user's own message stops being pending once the server acknowledges it.
    expect(state().messages[0].pending).toBe(false);
  });

  it('appends deltas to the message the start frame named, not the local id', async () => {
    // The reducer matches on `streamingMessageId`, which `start` rewrites. Get
    // that wrong and every token lands on a message nothing renders.
    await stream([
      START,
      { type: 'delta', text: 'Hello ' },
      { type: 'delta', text: 'world' },
      { type: 'done', finish_reason: 'stop' },
    ]);

    expect(assistant().content).toBe('Hello world');
    expect(state().messages.filter((m) => m.role === 'assistant')).toHaveLength(1);
  });

  it('leaves deltas that arrive BEFORE start on the local message rather than dropping them', async () => {
    await stream([
      { type: 'delta', text: 'early' },
      START,
      { type: 'delta', text: ' late' },
      { type: 'done', finish_reason: 'stop' },
    ]);

    // Both halves survive on the reconciled message — nothing is silently lost.
    expect(assistant().content).toBe('early late');
  });
});

describe('chat reducer — status and terminal state', () => {
  it('maps the retrieving/generating stages onto the UI status', async () => {
    const seen: string[] = [];
    const unsub = chatStore.subscribe((s) => seen.push(s.streamStatus));

    await stream([
      START,
      { type: 'status', stage: 'retrieving' },
      { type: 'status', stage: 'generating' },
      { type: 'delta', text: 'x' },
      { type: 'done', finish_reason: 'stop' },
    ]);
    unsub();

    expect(seen).toContain('retrieving');
    expect(seen).toContain('thinking');
    expect(seen).toContain('streaming');
    expect(state().streamStatus).toBe('done');
  });

  it('marks the message complete and clears pending on done', async () => {
    await stream([
      START,
      { type: 'delta', text: 'answer' },
      { type: 'done', finish_reason: 'stop' },
    ]);

    expect(assistant().status).toBe('complete');
    expect(assistant().pending).toBe(false);
    expect(state().streamingMessageId).toBeNull();
  });

  it('adopts the server-generated conversation title from the done frame', async () => {
    // The server names a conversation from its first question. Both the sidebar
    // row and the header read from different fields, so both must be updated.
    await stream([START, { type: 'done', finish_reason: 'stop', title: 'The Tuesday decision' }]);

    expect(state().activeConversation?.title).toBe('The Tuesday decision');
    expect(state().conversations.find((c) => c.uuid === 'conv-1')?.title).toBe(
      'The Tuesday decision'
    );
  });

  it('leaves the title alone when the done frame carries none', async () => {
    await stream([START, { type: 'done', finish_reason: 'stop' }]);

    expect(state().activeConversation?.title).toBe('');
  });
});

describe('chat reducer — error handling preserves partial text', () => {
  it('KEEPS the partial answer when an error frame arrives mid-stream', async () => {
    // Wiping content here is the worst available behaviour: the user loses text
    // they were reading and gets no explanation of what happened to it.
    await stream([
      START,
      { type: 'delta', text: 'They decided to ' },
      { type: 'error', code: 'provider_error', message: 'Upstream model failed' },
      { type: 'done', finish_reason: 'error' },
    ]);

    expect(assistant().content).toBe('They decided to ');
    expect(assistant().status).toBe('error');
    expect(assistant().error).toBe('Upstream model failed');
    expect(state().error).toBe('provider_error');
  });

  it('a done frame after an error does NOT rewrite status back to complete', async () => {
    // The server always sends `done`. If it flipped status to 'complete' the
    // error text and the Retry button would both vanish, leaving a truncated
    // answer that looks finished.
    await stream([
      START,
      { type: 'delta', text: 'partial' },
      { type: 'error', code: 'provider_error', message: 'boom' },
      { type: 'done', finish_reason: 'error' },
    ]);

    expect(assistant().status).toBe('error');
    expect(assistant().error).toBe('boom');
    expect(assistant().pending).toBe(false);
  });

  it('a transport failure marks the message errored without clearing what streamed', async () => {
    await stream([START, { type: 'delta', text: 'half an answer' }], new Error('network down'));

    expect(assistant().content).toBe('half an answer');
    expect(assistant().status).toBe('error');
    expect(state().error).toBe('send');
    expect(state().streamStatus).toBe('error');
  });
});

describe('chat reducer — usage, sources and warnings', () => {
  it('records token usage on BOTH the store and the message', async () => {
    await stream([
      START,
      { type: 'delta', text: 'x' },
      {
        type: 'usage',
        prompt_tokens: 1200,
        completion_tokens: 340,
        total_tokens: 1540,
        estimated: false,
      },
      { type: 'done', finish_reason: 'stop' },
    ]);

    expect(state().tokenUsage).toEqual({
      prompt_tokens: 1200,
      completion_tokens: 340,
      total_tokens: 1540,
      estimated: false,
    });
    // The per-message copy is what survives a reload; the store copy is the live
    // counter. Recording only one leaves the other reading zero.
    expect(assistant().total_tokens).toBe(1540);
    expect(assistant().tokens_estimated).toBe(false);
  });

  it('attaches citations from the sources frame to the streaming message', async () => {
    const citations = [
      { index: 1, file_uuid: 'f1', filename: 'a.mp4', start_time: 12, end_time: 20 },
    ] as never;

    await stream([START, { type: 'sources', citations }, { type: 'done', finish_reason: 'stop' }]);

    expect(assistant().citations).toHaveLength(1);
  });

  it('folds a context_dropped warning into msg_metadata (one render path)', async () => {
    // Kept in msg_metadata rather than transient stream state so the notice
    // survives a page reload — see ChatMessage.contextDropped.test.ts.
    await stream([
      START,
      { type: 'warning', code: 'context_dropped', retrieved: 6 },
      { type: 'done', finish_reason: 'stop' },
    ]);

    expect(assistant().msg_metadata?.context_dropped).toBe(true);
  });

  it('folds a no_context warning, with the counts that explain it (#438)', async () => {
    // `retrieved` is what separates "the search came back empty" from "masking
    // dropped every chunk", and it is not otherwise on screen mid-stream.
    await stream([
      START,
      { type: 'warning', code: 'no_context', retrieved: 0, files_searched: 'all' },
      { type: 'done', finish_reason: 'stop' },
    ]);

    expect(assistant().msg_metadata?.no_context).toBe(true);
    expect(assistant().msg_metadata?.retrieved).toBe(0);
    expect(assistant().msg_metadata?.files_searched).toBe('all');
  });

  it('does not set no_context on a context_dropped warning', async () => {
    // The two codes name different defects; conflating them would tell the user
    // nothing was searched when excerpts were found and then discarded.
    await stream([
      START,
      { type: 'warning', code: 'context_dropped', retrieved: 6 },
      { type: 'done', finish_reason: 'stop' },
    ]);

    expect(assistant().msg_metadata?.no_context).toBeUndefined();
  });

  it('ignores an unrecognised warning code instead of throwing', async () => {
    await stream([
      START,
      { type: 'warning', code: 'something_new' as never },
      { type: 'done', finish_reason: 'stop' },
    ]);

    expect(assistant().status).toBe('complete');
  });
});

describe('chat reducer — reasoning phase', () => {
  it('accumulates reasoning text separately from the answer', async () => {
    await stream([
      START,
      { type: 'reasoning', text: 'Let me check ' },
      { type: 'reasoning', text: 'the transcript.' },
      { type: 'delta', text: 'They shipped Tuesday.' },
      { type: 'done', finish_reason: 'stop' },
    ]);

    expect(assistant().reasoning_content).toBe('Let me check the transcript.');
    // Reasoning must never leak into the answer bubble.
    expect(assistant().content).toBe('They shipped Tuesday.');
  });

  it('starts the reasoning clock ONCE, not on every frame', async () => {
    // Resetting `reasoningStartedAt` per frame makes the live elapsed counter
    // stick near zero for the whole reasoning phase. Time must pass BETWEEN
    // frames for that to be observable, hence the manual clock advance.
    const nowSpy = vi.spyOn(Date, 'now');
    nowSpy.mockReturnValue(1_000);

    streamChatMessage.mockImplementationOnce(async (_u, _p, onEvent) => {
      onEvent(START);
      onEvent({ type: 'reasoning', text: 'a' });
      const afterFirst = assistant().reasoningStartedAt;
      nowSpy.mockReturnValue(6_000); // 5s later
      onEvent({ type: 'reasoning', text: 'b' });
      // Unchanged despite the clock moving 5s — this is the assertion that fails
      // if `reasoningStartedAt` is re-set per frame.
      expect(assistant().reasoningStartedAt).toBe(afterFirst);
      nowSpy.mockReturnValue(9_000); // 8s after the first reasoning frame
      onEvent({ type: 'delta', text: 'answer' });
      onEvent({ type: 'done', finish_reason: 'stop' });
    });

    await chatStore.sendMessage('think about it');

    // 9000 - 1000: the whole reasoning phase, not just the last frame's slice.
    expect(assistant().reasoningDurationMs).toBe(8_000);
    nowSpy.mockRestore();
  });

  it('the first answer delta ends the reasoning phase and freezes its duration', async () => {
    await stream([
      START,
      { type: 'reasoning', text: 'thinking' },
      { type: 'delta', text: 'answer' },
      { type: 'done', finish_reason: 'stop' },
    ]);

    expect(assistant().reasoningStreaming).toBe(false);
    expect(assistant().reasoningDurationMs).toBeTypeOf('number');
  });

  it('freezes the duration on done for a turn that reasoned but never answered', async () => {
    await stream([
      START,
      { type: 'reasoning', text: 'thinking' },
      { type: 'done', finish_reason: 'stop' },
    ]);

    expect(assistant().reasoningStreaming).toBe(false);
    expect(assistant().reasoningDurationMs).toBeTypeOf('number');
  });
});

describe('chat store — send guards and stopGeneration', () => {
  it('ignores an empty or whitespace-only message', async () => {
    await chatStore.sendMessage('   ');

    expect(state().messages).toEqual([]);
    expect(streamChatMessage).not.toHaveBeenCalled();
  });

  it('stopGeneration aborts the stream signal', async () => {
    scriptedFrames = [START, { type: 'delta', text: 'partial' }];
    streamError = null;

    // Abort from inside the stream, at the point Stop would be pressed.
    const scripted = scriptedFrames;
    streamChatMessage.mockImplementationOnce(async (_u, _p, onEvent, signal) => {
      lastSignal = signal;
      onEvent(scripted[0]);
      onEvent(scripted[1]);
      chatStore.stopGeneration();
      expect(signal.aborted).toBe(true);
      throw Object.assign(new Error('aborted'), { name: 'AbortError' });
    });

    await chatStore.sendMessage('stop me');

    expect(lastSignal?.aborted).toBe(true);
    expect(state().streamStatus).toBe('aborted');
    // Cancelling must keep what already streamed — the server persists the same
    // partial content, so discarding it here would disagree with a reload.
    expect(assistant().content).toBe('partial');
    expect(assistant().status).toBe('cancelled');
  });

  it('reset() aborts an in-flight stream and clears renderable state', async () => {
    await stream([START, { type: 'delta', text: 'x' }, { type: 'done', finish_reason: 'stop' }]);
    expect(state().messages).toHaveLength(2);

    chatStore.reset();

    expect(state().messages).toEqual([]);
    expect(state().streamStatus).toBe('idle');
    expect(state().tokenUsage).toBeNull();
    expect(state().error).toBeNull();
  });

  it('clears the previous turn’s usage and error when a new turn starts', async () => {
    await stream([
      START,
      { type: 'error', code: 'provider_error', message: 'boom' },
      { type: 'done', finish_reason: 'error' },
    ]);
    expect(state().error).toBe('provider_error');

    await stream([START, { type: 'delta', text: 'ok' }, { type: 'done', finish_reason: 'stop' }]);

    expect(state().error).toBeNull();
  });
});
