/**
 * SSE frame parsing for chat streaming (issue #52).
 *
 * The parser has to survive what a real network does to a byte stream: frames
 * split anywhere, CRLF endings, keepalive comments, and events from a newer
 * backend it has never heard of.
 */

import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';

// The edition is a build-time constant; a getter over hoisted state lets one file
// exercise both the external-auth and the local-auth paths (issue #1035).
const auth = vi.hoisted(() => ({
  external: false,
  getSessionToken: vi.fn<() => Promise<string | null>>(),
}));
vi.mock('$lib/edition', () => ({
  get isCloudEdition() {
    return auth.external;
  },
}));
vi.mock('$lib/cloud', () => ({ getSessionToken: auth.getSessionToken }));

import axiosInstance from '$lib/axios';
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

  it('passes a no_context warning through with its counts (#438)', () => {
    const events: ChatStreamEvent[] = [];
    const parser = createSseParser((event) => events.push(event));

    parser.push(
      'event: warning\ndata: {"code":"no_context","retrieved":0,"files_searched":"all"}\n\n'
    );

    expect(events[0]).toEqual({
      type: 'warning',
      code: 'no_context',
      retrieved: 0,
      files_searched: 'all',
    });
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
    // `getCsrfToken()` reads document.cookie, so give it a known value to carry through.
    // Without this the header is '' and the old `toBeDefined()` still passed (issue #431).
    document.cookie = 'csrf_token=test-csrf-value';

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
    // `chatStream.ts` sends `getCsrfToken() ?? ''`, so a missing token yields an EMPTY
    // header — and `toBeDefined()` passes on ''. This test is named "sends the CSRF header"
    // yet would have passed while sending no token at all. Assert it carries the cookie's
    // actual value, which is the only form that can fail (issue #431).
    expect(init.headers['X-CSRF-Token']).toBe('test-csrf-value');
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

/** A 200 response whose body is one `done` frame. */
function okStream() {
  return {
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
  };
}

function unauthorized() {
  return { ok: false, status: 401, json: async () => ({ detail: 'Not authenticated' }) };
}

async function send(): Promise<ChatStreamEvent[]> {
  const { streamChatMessage } = await import('./chatStream');
  const events: ChatStreamEvent[] = [];
  await streamChatMessage(
    'conv-1',
    { content: 'hello' },
    (e) => events.push(e),
    new AbortController().signal
  );
  return events;
}

describe('streamChatMessage auth (issue #1035)', () => {
  let refreshSpy: ReturnType<typeof vi.spyOn>;

  beforeEach(() => {
    auth.getSessionToken.mockReset();
    document.cookie = 'csrf_token=test-csrf-value';
    refreshSpy = vi.spyOn(axiosInstance, 'post').mockResolvedValue({ data: {} });
  });

  afterEach(() => {
    auth.external = false;
    refreshSpy.mockRestore();
    vi.unstubAllGlobals();
  });

  describe('external auth', () => {
    beforeEach(() => {
      auth.external = true;
    });

    it('sends the external session bearer the axios interceptor would send', async () => {
      auth.getSessionToken.mockResolvedValue('ext-token-1');
      const fetchMock = vi.fn().mockResolvedValue(okStream());
      vi.stubGlobal('fetch', fetchMock);

      const events = await send();

      const [, init] = fetchMock.mock.calls[0];
      expect(init.headers['Authorization']).toBe('Bearer ext-token-1');
      // Bearer auth does not use the cookie CSRF double-submit.
      expect(init.headers['X-CSRF-Token']).toBeUndefined();
      expect(events).toEqual([{ type: 'done', finish_reason: 'stop' }]);
    });

    it('on 401 retries once with a freshly minted token, never the cookie refresh', async () => {
      auth.getSessionToken
        .mockResolvedValueOnce('expired-token')
        .mockResolvedValueOnce('fresh-token');
      const fetchMock = vi
        .fn()
        .mockResolvedValueOnce(unauthorized())
        .mockResolvedValueOnce(okStream());
      vi.stubGlobal('fetch', fetchMock);

      const events = await send();

      expect(fetchMock).toHaveBeenCalledTimes(2);
      expect(fetchMock.mock.calls[0][1].headers['Authorization']).toBe('Bearer expired-token');
      expect(fetchMock.mock.calls[1][1].headers['Authorization']).toBe('Bearer fresh-token');
      expect(refreshSpy).not.toHaveBeenCalled();
      expect(events).toEqual([{ type: 'done', finish_reason: 'stop' }]);
    });

    it('surfaces a second 401 as an error event after exactly one retry', async () => {
      auth.getSessionToken.mockResolvedValue('rejected-token');
      const fetchMock = vi.fn().mockResolvedValue(unauthorized());
      vi.stubGlobal('fetch', fetchMock);

      const events = await send();

      expect(fetchMock).toHaveBeenCalledTimes(2);
      expect(refreshSpy).not.toHaveBeenCalled();
      expect(events).toEqual([
        { type: 'error', code: 'provider_error', message: 'Not authenticated' },
      ]);
    });
  });

  describe('local auth (control: behaviour unchanged)', () => {
    it('sends the CSRF header and no bearer, without asking for a session token', async () => {
      const fetchMock = vi.fn().mockResolvedValue(okStream());
      vi.stubGlobal('fetch', fetchMock);

      await send();

      const [, init] = fetchMock.mock.calls[0];
      expect(init.headers['X-CSRF-Token']).toBe('test-csrf-value');
      expect(init.headers['Authorization']).toBeUndefined();
      expect(init.credentials).toBe('same-origin');
      expect(auth.getSessionToken).not.toHaveBeenCalled();
    });

    it('on 401 refreshes the cookie session, then retries once', async () => {
      const fetchMock = vi
        .fn()
        .mockResolvedValueOnce(unauthorized())
        .mockResolvedValueOnce(okStream());
      vi.stubGlobal('fetch', fetchMock);

      const events = await send();

      expect(refreshSpy).toHaveBeenCalledExactlyOnceWith('/auth/token/refresh', {});
      expect(fetchMock).toHaveBeenCalledTimes(2);
      expect(events).toEqual([{ type: 'done', finish_reason: 'stop' }]);
    });
  });
});
