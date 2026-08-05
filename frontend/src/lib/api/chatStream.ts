/**
 * SSE client for streaming chat replies (issue #52).
 *
 * Uses **fetch + ReadableStream, deliberately not EventSource**:
 *  - the message body is POSTed, so prompts never land in URLs or access logs;
 *  - `AbortController` gives us a real stop-generation control;
 *  - EventSource auto-reconnects, which here would silently re-trigger a whole
 *    (billed) generation.
 *
 * Raw fetch bypasses the axios interceptors, so CSRF and the 401-refresh dance
 * are handled explicitly below.
 */

import axiosInstance, { getCsrfToken } from '$lib/axios';
import type { ChatStreamEvent, SendMessageRequest } from '$lib/types/chat';

/** Watchdog: no bytes at all for this long means the stream is wedged. */
const INACTIVITY_TIMEOUT_MS = 120_000;

/**
 * Incremental SSE frame parser.
 *
 * Exported as a pure function so it can be unit-tested against chunk splits
 * that a real network produces — frames arriving mid-line, `\r\n` endings,
 * multi-line `data:` payloads, and unknown event types from a newer server.
 */
export function createSseParser(onEvent: (event: ChatStreamEvent) => void) {
  let buffer = '';

  function flushFrame(frame: string): void {
    let eventName = 'message';
    const dataLines: string[] = [];

    for (const rawLine of frame.split(/\r?\n/)) {
      if (!rawLine || rawLine.startsWith(':')) continue; // blank or keepalive comment
      if (rawLine.startsWith('event:')) {
        eventName = rawLine.slice(6).trim();
      } else if (rawLine.startsWith('data:')) {
        dataLines.push(rawLine.slice(5).trimStart());
      }
    }

    if (dataLines.length === 0) return;

    let payload: Record<string, unknown>;
    try {
      payload = JSON.parse(dataLines.join('\n'));
    } catch {
      // A malformed frame must not kill the stream.
      return;
    }

    // Forward-compatible: an unknown event from a newer backend is ignored
    // rather than treated as an error.
    const known = ['start', 'status', 'sources', 'delta', 'usage', 'done', 'error'];
    if (!known.includes(eventName)) return;

    onEvent({ type: eventName, ...payload } as ChatStreamEvent);
  }

  return {
    push(chunk: string): void {
      buffer += chunk;
      // Frames are separated by a blank line; handle both LF and CRLF.
      let separator = buffer.search(/\r?\n\r?\n/);
      while (separator !== -1) {
        const frame = buffer.slice(0, separator);
        const match = buffer.slice(separator).match(/^\r?\n\r?\n/);
        buffer = buffer.slice(separator + (match ? match[0].length : 2));
        flushFrame(frame);
        separator = buffer.search(/\r?\n\r?\n/);
      }
    },
    /** Flush a trailing frame that arrived without its blank-line terminator. */
    end(): void {
      if (buffer.trim()) {
        flushFrame(buffer);
        buffer = '';
      }
    },
  };
}

async function postStream(url: string, body: unknown, signal: AbortSignal): Promise<Response> {
  return fetch(url, {
    method: 'POST',
    headers: {
      'Content-Type': 'application/json',
      'X-CSRF-Token': getCsrfToken() ?? '',
    },
    credentials: 'same-origin',
    body: JSON.stringify(body ?? {}),
    signal,
  });
}

async function readStream(
  response: Response,
  onEvent: (event: ChatStreamEvent) => void,
  signal: AbortSignal
): Promise<void> {
  const reader = response.body?.getReader();
  if (!reader) throw new Error('Streaming is not supported by this browser.');

  const decoder = new TextDecoder();
  const parser = createSseParser(onEvent);

  let watchdog: ReturnType<typeof setTimeout> | undefined;
  const resetWatchdog = () => {
    if (watchdog) clearTimeout(watchdog);
    watchdog = setTimeout(() => {
      // A hung stream must not wedge the composer forever.
      reader.cancel().catch(() => undefined);
    }, INACTIVITY_TIMEOUT_MS);
  };

  const onAbort = () => reader.cancel().catch(() => undefined);
  signal.addEventListener('abort', onAbort);

  try {
    resetWatchdog();
    for (;;) {
      const { done, value } = await reader.read();
      if (done) break;
      resetWatchdog();
      parser.push(decoder.decode(value, { stream: true }));
    }
    parser.push(decoder.decode());
    parser.end();
  } finally {
    if (watchdog) clearTimeout(watchdog);
    signal.removeEventListener('abort', onAbort);
    reader.releaseLock?.();
  }
}

async function errorFromResponse(response: Response): Promise<ChatStreamEvent> {
  let message = `Request failed (${response.status})`;
  try {
    const body = await response.json();
    if (body?.detail) message = String(body.detail);
  } catch {
    // Non-JSON error body — keep the status-based message.
  }

  if (response.status === 402) return { type: 'error', code: 'quota_exceeded', message };
  if (response.status === 429) return { type: 'error', code: 'rate_limited', message };
  if (response.status === 400 && /llm/i.test(message)) {
    return { type: 'error', code: 'llm_unconfigured', message };
  }
  return { type: 'error', code: 'provider_error', message };
}

async function streamPost(
  url: string,
  body: unknown,
  onEvent: (event: ChatStreamEvent) => void,
  signal: AbortSignal
): Promise<void> {
  let response = await postStream(url, body, signal);

  // Raw fetch misses the axios 401-refresh interceptor; do it once by hand so a
  // token that expired mid-session doesn't drop the user out of a conversation.
  if (response.status === 401) {
    try {
      await axiosInstance.post('/auth/token/refresh', {});
      response = await postStream(url, body, signal);
    } catch {
      // Fall through: the second 401 below hands off to the normal auth flow.
    }
  }

  if (!response.ok) {
    onEvent(await errorFromResponse(response));
    return;
  }

  await readStream(response, onEvent, signal);
}

/**
 * Send a message and stream the assistant's reply.
 *
 * Resolves when the stream ends (including on an in-band `error` frame). Aborts
 * surface as an `AbortError` rejection so the caller can distinguish a user stop
 * from a failure.
 */
export async function streamChatMessage(
  conversationUuid: string,
  payload: SendMessageRequest,
  onEvent: (event: ChatStreamEvent) => void,
  signal: AbortSignal
): Promise<void> {
  await streamPost(
    `/api/chat/conversations/${encodeURIComponent(conversationUuid)}/messages`,
    payload,
    onEvent,
    signal
  );
}

/**
 * Rewrite an earlier question and stream a fresh answer from that point.
 *
 * The server supersedes the edited turn and everything after it, so the reply
 * is generated against the corrected question with the later (now invalid)
 * exchanges excluded from history.
 */
export async function streamEditMessage(
  conversationUuid: string,
  messageUuid: string,
  content: string,
  onEvent: (event: ChatStreamEvent) => void,
  signal: AbortSignal
): Promise<void> {
  await streamPost(
    `/api/chat/conversations/${encodeURIComponent(conversationUuid)}/messages/` +
      `${encodeURIComponent(messageUuid)}/edit`,
    { content },
    onEvent,
    signal
  );
}

/** Re-answer the last question in a conversation, streaming the new reply. */
export async function streamRegenerate(
  conversationUuid: string,
  onEvent: (event: ChatStreamEvent) => void,
  signal: AbortSignal
): Promise<void> {
  await streamPost(
    `/api/chat/conversations/${encodeURIComponent(conversationUuid)}/regenerate`,
    {},
    onEvent,
    signal
  );
}
