/**
 * `chatStore`'s fold of the `retrieval_failed` warning code (issue #438's open
 * half). Self-contained rather than added to `chat.reducer.test.ts`, mirroring
 * that file's harness (mocked `chatApi`/`chatStream`, driven through the real
 * `sendMessage()` so the frames pass through the real optimistic-turn
 * reconciliation) but scoped to exactly this one code's fold, per this repo's
 * "new test files, distinctly named" convention for parallel-lane work.
 *
 * `chat.reducer.test.ts` already pins `no_context` and `context_dropped`; this
 * file pins their sibling and the property that makes it useful — it survives
 * in `msg_metadata` distinctly from `no_context`, so `ChatMessage` can render a
 * different notice for "search was down" than for "your library has nothing
 * about this".
 */

import { describe, it, expect, vi, beforeEach } from 'vitest';
import { get } from 'svelte/store';

vi.mock('$lib/api/chatStream', async () =>
  (await import('$testMocks/chatStreamHarness')).chatStreamMock()
);
vi.mock('$lib/api/chatApi', async () =>
  (await import('$testMocks/chatStreamHarness')).chatApiMock()
);

import * as chatApi from '$lib/api/chatApi';
import { chatStore } from './chat';
import { START, ASSISTANT_UUID, createChatHarness } from '$testMocks/chatStreamHarness';

const { stream, prepare } = createChatHarness(chatStore, chatApi);

function assistant() {
  const message = get(chatStore).messages.find((m) => m.uuid === ASSISTANT_UUID);
  if (!message) throw new Error('assistant message not found');
  return message;
}

beforeEach(prepare);

describe('chat reducer — retrieval_failed warning fold', () => {
  it('folds a retrieval_failed warning, with the counts that explain it', async () => {
    await stream([
      START,
      { type: 'warning', code: 'retrieval_failed', retrieved: 0, files_searched: 'all' },
      { type: 'done', finish_reason: 'stop' },
    ]);

    expect(assistant().msg_metadata?.retrieval_failed).toBe(true);
    expect(assistant().msg_metadata?.retrieved).toBe(0);
    expect(assistant().msg_metadata?.files_searched).toBe('all');
  });

  it('does not set no_context on a retrieval_failed warning', async () => {
    // Mutually exclusive server-side codes must stay mutually exclusive
    // client-side too, or ChatMessage would have to guess which to render.
    await stream([
      START,
      { type: 'warning', code: 'retrieval_failed', retrieved: 0, files_searched: 'all' },
      { type: 'done', finish_reason: 'stop' },
    ]);

    expect(assistant().msg_metadata?.no_context).toBeUndefined();
  });

  it('does not set retrieval_failed on a plain no_context warning', async () => {
    // The control: the ordinary #438 fold is unaffected by the new code.
    await stream([
      START,
      { type: 'warning', code: 'no_context', retrieved: 0, files_searched: 'all' },
      { type: 'done', finish_reason: 'stop' },
    ]);

    expect(assistant().msg_metadata?.no_context).toBe(true);
    expect(assistant().msg_metadata?.retrieval_failed).toBeUndefined();
  });

  it('persists retrieval_failed in msg_metadata rather than separate stream state', async () => {
    // Same rule as every other warning code: the flag lives on the message, so
    // it survives a reload rather than existing only for the streaming session.
    await stream([
      START,
      { type: 'warning', code: 'retrieval_failed', retrieved: 0, files_searched: 'all' },
      { type: 'delta', text: 'Fallback answer.' },
      { type: 'done', finish_reason: 'stop' },
    ]);

    expect(assistant().msg_metadata?.retrieval_failed).toBe(true);
    expect(assistant().content).toBe('Fallback answer.');
  });
});
