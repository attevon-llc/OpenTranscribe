/**
 * Regression guard for #788 — surfacing `Retry-After` in the chat error path.
 *
 * `chatStream.ts`'s `errorFromResponse` parses a 429's `Retry-After` header
 * into `retryAfter` on the SSE error event; `stores/chat.ts`'s reducer copies
 * it (plus `errorCode`) onto the errored message. This component renders a
 * "try again in..." hint only when BOTH are present, so an ordinary provider
 * error (no retry-after, or a different error code entirely) never shows a
 * fabricated wait time.
 */
import { describe, expect, it } from 'vitest';
import { render } from '@testing-library/svelte';

import ChatMessage from './ChatMessage.svelte';
import type { ChatMessage as ChatMessageType } from '$lib/types/chat';

function assistantMessage(overrides: Partial<ChatMessageType> = {}): ChatMessageType {
  return {
    uuid: 'assistant-1',
    role: 'assistant',
    content: '',
    status: 'error',
    ...overrides,
  };
}

describe('ChatMessage — rate-limit retry-after hint (#788)', () => {
  it('shows a wait hint for a rate_limited error carrying retryAfter', () => {
    const { getByTestId } = render(ChatMessage, {
      props: {
        message: assistantMessage({
          error: 'You have sent a lot of messages — please wait a moment.',
          errorCode: 'rate_limited',
          retryAfter: 12,
        }),
      },
    });

    expect(getByTestId('chat-message-error')).toBeTruthy();
    // i18next resources aren't loaded in this unit-test environment, so `$t`
    // falls back to the raw key (see `stores/locale.ts`) -- assert on THAT,
    // which still proves the component picked the seconds key (not minutes)
    // for a sub-60s wait, i.e. the branching logic under test, not the prose.
    expect(getByTestId('chat-message-retry-after').textContent).toContain(
      'common.retryAfterSeconds'
    );
  });

  it('degrades to the generic error with no hint when retryAfter is absent', () => {
    // The issue's own acceptance criterion: an unparseable/absent header must
    // degrade gracefully, never show a fabricated or NaN wait time.
    const { getByTestId, queryByTestId } = render(ChatMessage, {
      props: {
        message: assistantMessage({
          error: 'You have sent a lot of messages — please wait a moment.',
          errorCode: 'rate_limited',
        }),
      },
    });

    expect(getByTestId('chat-message-error')).toBeTruthy();
    expect(queryByTestId('chat-message-retry-after')).toBeNull();
  });

  it('does not show a retry-after hint for a non-rate-limited error', () => {
    const { queryByTestId } = render(ChatMessage, {
      props: {
        message: assistantMessage({
          error: 'The provider returned an error.',
          errorCode: 'provider_error',
          // A stray retryAfter on the wrong code must not leak through.
          retryAfter: 12,
        }),
      },
    });

    expect(queryByTestId('chat-message-retry-after')).toBeNull();
  });
});
