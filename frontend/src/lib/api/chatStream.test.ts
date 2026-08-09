/**
 * SSE frame parsing for chat streaming (issue #52).
 *
 * The parser has to survive what a real network does to a byte stream: frames
 * split anywhere, CRLF endings, keepalive comments, and events from a newer
 * backend it has never heard of.
 */

import { describe, expect, it, vi } from 'vitest';

import { createSseParser } from './chatStream';
import type { ChatStreamEvent } from '$lib/types/chat';

function collect(): { events: ChatStreamEvent[]; onEvent: (e: ChatStreamEvent) => void } {
  const events: ChatStreamEvent[] = [];
  return { events, onEvent: (e) => events.push(e) };
}

describe('createSseParser', () => {
  it('parses a complete frame', () => {
    const { events, onEvent } = collect();
    const parser = createSseParser(onEvent);

    parser.push('event: delta\ndata: {"text":"hello"}\n\n');

    expect(events).toEqual([{ type: 'delta', text: 'hello' }]);
  });

  it('parses multiple frames in one chunk', () => {
    const { events, onEvent } = collect();
    const parser = createSseParser(onEvent);

    parser.push('event: delta\ndata: {"text":"a"}\n\nevent: delta\ndata: {"text":"b"}\n\n');

    expect(events.map((e) => (e as { text: string }).text)).toEqual(['a', 'b']);
  });

  it('reassembles a frame split mid-line across chunks', () => {
    const { events, onEvent } = collect();
    const parser = createSseParser(onEvent);

    parser.push('event: del');
    parser.push('ta\ndata: {"te');
    parser.push('xt":"split"}\n\n');

    expect(events).toEqual([{ type: 'delta', text: 'split' }]);
  });

  it('handles a frame split exactly on the blank-line separator', () => {
    const { events, onEvent } = collect();
    const parser = createSseParser(onEvent);

    parser.push('event: delta\ndata: {"text":"x"}\n');
    parser.push('\n');

    expect(events).toHaveLength(1);
  });

  it('handles CRLF line endings', () => {
    const { events, onEvent } = collect();
    const parser = createSseParser(onEvent);

    parser.push('event: delta\r\ndata: {"text":"crlf"}\r\n\r\n');

    expect(events).toEqual([{ type: 'delta', text: 'crlf' }]);
  });

  it('joins multi-line data payloads', () => {
    const { events, onEvent } = collect();
    const parser = createSseParser(onEvent);

    parser.push('event: delta\ndata: {"text":\ndata: "multi"}\n\n');

    expect(events).toEqual([{ type: 'delta', text: 'multi' }]);
  });

  it('ignores keepalive comments', () => {
    const { events, onEvent } = collect();
    const parser = createSseParser(onEvent);

    parser.push(': keepalive\n\n');
    parser.push('event: delta\ndata: {"text":"after"}\n\n');

    expect(events).toEqual([{ type: 'delta', text: 'after' }]);
  });

  it('tolerates malformed JSON without dropping the stream', () => {
    const { events, onEvent } = collect();
    const parser = createSseParser(onEvent);

    parser.push('event: delta\ndata: {not json\n\n');
    parser.push('event: delta\ndata: {"text":"recovered"}\n\n');

    expect(events).toEqual([{ type: 'delta', text: 'recovered' }]);
  });

  it('ignores unknown event types from a newer backend', () => {
    const { events, onEvent } = collect();
    const parser = createSseParser(onEvent);

    parser.push('event: some_future_event\ndata: {"x":1}\n\n');
    parser.push('event: delta\ndata: {"text":"known"}\n\n');

    expect(events).toEqual([{ type: 'delta', text: 'known' }]);
  });

  it('parses the full frame vocabulary', () => {
    const { events, onEvent } = collect();
    const parser = createSseParser(onEvent);

    parser.push(
      'event: start\ndata: {"conversation_uuid":"c","user_message_uuid":"u","assistant_message_uuid":"a"}\n\n' +
        'event: status\ndata: {"stage":"retrieving"}\n\n' +
        'event: sources\ndata: {"citations":[{"id":1,"file_uuid":"f","title":"T","chunk_index":0,"start_time":10,"end_time":20,"speaker":"Dana","snippet":"s"}]}\n\n' +
        'event: warning\ndata: {"code":"context_dropped","retrieved":3}\n\n' +
        'event: reasoning\ndata: {"text":"thinking..."}\n\n' +
        'event: delta\ndata: {"text":"answer"}\n\n' +
        'event: usage\ndata: {"prompt_tokens":10,"completion_tokens":5,"total_tokens":15,"estimated":false}\n\n' +
        'event: done\ndata: {"finish_reason":"stop","title":"A title"}\n\n'
    );

    expect(events.map((e) => e.type)).toEqual([
      'start',
      'status',
      'sources',
      'warning',
      'reasoning',
      'delta',
      'usage',
      'done',
    ]);
    const sources = events[2] as { citations: unknown[] };
    expect(sources.citations).toHaveLength(1);
  });

  it('parses the warning frame the server sends when context was dropped', () => {
    // Issue #384: retrieval found excerpts but none fit the prompt budget. The
    // parser must forward this rather than treat it as an unknown future event,
    // or the user reads an ungrounded answer as a normal one.
    const { events, onEvent } = collect();
    const parser = createSseParser(onEvent);

    parser.push('event: warning\ndata: {"code":"context_dropped","retrieved":4}\n\n');

    expect(events[0]).toEqual({ type: 'warning', code: 'context_dropped', retrieved: 4 });
  });

  it('parses reasoning frames, distinct from delta', () => {
    const { events, onEvent } = collect();
    const parser = createSseParser(onEvent);

    parser.push(
      'event: reasoning\ndata: {"text":"considering the options"}\n\n' +
        'event: delta\ndata: {"text":"the final answer"}\n\n'
    );

    expect(events).toEqual([
      { type: 'reasoning', text: 'considering the options' },
      { type: 'delta', text: 'the final answer' },
    ]);
  });

  it('parses error frames with their code', () => {
    const { events, onEvent } = collect();
    const parser = createSseParser(onEvent);

    parser.push('event: error\ndata: {"code":"quota_exceeded","message":"over limit"}\n\n');

    expect(events[0]).toEqual({
      type: 'error',
      code: 'quota_exceeded',
      message: 'over limit',
    });
  });

  it('flushes a trailing frame that never got its blank line', () => {
    const { events, onEvent } = collect();
    const parser = createSseParser(onEvent);

    parser.push('event: done\ndata: {"finish_reason":"stop"}');
    parser.end();

    expect(events).toEqual([{ type: 'done', finish_reason: 'stop' }]);
  });

  it('emits nothing for a frame with no data lines', () => {
    const { events, onEvent } = collect();
    const parser = createSseParser(onEvent);

    parser.push('event: delta\n\n');

    expect(events).toEqual([]);
  });

  it('accumulates a long streamed answer in order', () => {
    const { events, onEvent } = collect();
    const parser = createSseParser(onEvent);

    const words = ['The', ' budget', ' was', ' approved'];
    for (const word of words) {
      parser.push(`event: delta\ndata: ${JSON.stringify({ text: word })}\n\n`);
    }

    const text = events.map((e) => (e as { text: string }).text).join('');
    expect(text).toBe('The budget was approved');
  });
});

describe('streamChatMessage', () => {
  it('POSTs the message body and sends the CSRF header', async () => {
    const { streamChatMessage } = await import('./chatStream');

    const fetchMock = vi.fn().mockResolvedValue({
      ok: true,
      status: 200,
      body: {
        getReader: () => ({
          read: vi
            .fn()
            .mockResolvedValueOnce({
              done: false,
              value: new TextEncoder().encode('event: done\ndata: {"finish_reason":"stop"}\n\n'),
            })
            .mockResolvedValueOnce({ done: true, value: undefined }),
          cancel: vi.fn().mockResolvedValue(undefined),
          releaseLock: vi.fn(),
        }),
      },
    });
    vi.stubGlobal('fetch', fetchMock);

    const events: ChatStreamEvent[] = [];
    await streamChatMessage(
      'conv-1',
      { content: 'hello' },
      (e) => events.push(e),
      new AbortController().signal
    );

    expect(fetchMock).toHaveBeenCalledOnce();
    const [url, init] = fetchMock.mock.calls[0];
    expect(url).toBe('/api/chat/conversations/conv-1/messages');
    expect(init.method).toBe('POST');
    // Prompt text goes in the body, never the URL.
    expect(url).not.toContain('hello');
    expect(JSON.parse(init.body)).toEqual({ content: 'hello' });
    expect(init.headers['X-CSRF-Token']).toBeDefined();
    expect(events).toEqual([{ type: 'done', finish_reason: 'stop' }]);

    vi.unstubAllGlobals();
  });

  it('maps HTTP failures to typed error events', async () => {
    const { streamChatMessage } = await import('./chatStream');

    const fetchMock = vi.fn().mockResolvedValue({
      ok: false,
      status: 429,
      json: async () => ({ detail: 'Hourly chat limit reached.' }),
    });
    vi.stubGlobal('fetch', fetchMock);

    const events: ChatStreamEvent[] = [];
    await streamChatMessage(
      'conv-1',
      { content: 'hi' },
      (e) => events.push(e),
      new AbortController().signal
    );

    expect(events).toEqual([
      { type: 'error', code: 'rate_limited', message: 'Hourly chat limit reached.' },
    ]);

    vi.unstubAllGlobals();
  });

  it('maps a 402 to quota_exceeded', async () => {
    const { streamChatMessage } = await import('./chatStream');

    vi.stubGlobal(
      'fetch',
      vi.fn().mockResolvedValue({
        ok: false,
        status: 402,
        json: async () => ({ detail: 'Chat quota exceeded' }),
      })
    );

    const events: ChatStreamEvent[] = [];
    await streamChatMessage(
      'c',
      { content: 'hi' },
      (e) => events.push(e),
      new AbortController().signal
    );

    expect((events[0] as { code: string }).code).toBe('quota_exceeded');
    vi.unstubAllGlobals();
  });
});
