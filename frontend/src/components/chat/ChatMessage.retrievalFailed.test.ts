/**
 * Regression guard for the FAILED-search notice (issue #438's open half).
 *
 * Before this, a failed OpenSearch query and a genuinely empty library both
 * rendered as `chat-no-context` — the user could not tell "your library has
 * nothing about this" from "search is temporarily unavailable". The server now
 * distinguishes them via `msg_metadata.retrieval_failed`, mutually exclusive
 * with `no_context`, and this component renders a DIFFERENT message for it.
 *
 * Mirrors `ChatMessage.contextDropped.test.ts`'s structure for the sibling
 * notices — same persisted-metadata-only render path, so the notice survives
 * a page reload rather than existing only for the streaming session.
 */
import { describe, expect, it } from 'vitest';
import { render } from '@testing-library/svelte';

import ChatMessage from './ChatMessage.svelte';
import type { ChatMessage as ChatMessageType } from '$lib/types/chat';

function assistantMessage(overrides: Partial<ChatMessageType> = {}): ChatMessageType {
  return {
    uuid: 'assistant-1',
    role: 'assistant',
    content: "I don't have enough information in the provided excerpts.",
    status: 'complete',
    ...overrides,
  };
}

describe('ChatMessage — search-unavailable notice (#438)', () => {
  it('warns with the FAILED-search message when retrieval_failed is set', () => {
    const { getByTestId } = render(ChatMessage, {
      props: {
        message: assistantMessage({
          msg_metadata: { retrieved: 0, files_searched: 'all', retrieval_failed: true },
        }),
      },
    });

    expect(getByTestId('chat-retrieval-failed')).toBeTruthy();
  });

  it('does NOT render the empty-library notice when retrieval_failed is set', () => {
    // The two must never stack — they name different defects, and the server
    // sets exactly one of the two keys per turn.
    const { queryByTestId, getByTestId } = render(ChatMessage, {
      props: {
        message: assistantMessage({
          msg_metadata: { retrieved: 0, files_searched: 'all', retrieval_failed: true },
        }),
      },
    });

    expect(getByTestId('chat-retrieval-failed')).toBeTruthy();
    expect(queryByTestId('chat-no-context')).toBeNull();
  });

  it('still renders the empty-library notice when retrieval_failed is absent', () => {
    // The control: the ordinary #438 behaviour is unchanged for a genuine miss.
    const { getByTestId, queryByTestId } = render(ChatMessage, {
      props: {
        message: assistantMessage({
          msg_metadata: { retrieved: 0, files_searched: 'all', no_context: true },
        }),
      },
    });

    expect(getByTestId('chat-no-context')).toBeTruthy();
    expect(queryByTestId('chat-retrieval-failed')).toBeNull();
  });

  it('prefers the context-dropped notice when both would otherwise apply', () => {
    // Cannot happen server-side (mutually exclusive branches of one `if`), but
    // the component's own precedence must still resolve to exactly one notice.
    const { getByTestId, queryByTestId } = render(ChatMessage, {
      props: {
        message: assistantMessage({
          msg_metadata: { retrieved: 6, context_dropped: true, retrieval_failed: true },
        }),
      },
    });

    expect(getByTestId('chat-context-dropped')).toBeTruthy();
    expect(queryByTestId('chat-retrieval-failed')).toBeNull();
  });

  it('stays silent on an ordinary grounded answer', () => {
    const { queryByTestId } = render(ChatMessage, {
      props: {
        message: assistantMessage({
          msg_metadata: { retrieved: 6, chunks_used: 6 },
        }),
      },
    });

    expect(queryByTestId('chat-retrieval-failed')).toBeNull();
  });

  it('renders the notice from persisted metadata, not from stream state', () => {
    // A reloaded thread has no `warning` frame to replay — only the DB row.
    const reloaded = assistantMessage({
      pending: false,
      msg_metadata: { retrieval_failed: true },
    });
    const { getByTestId } = render(ChatMessage, { props: { message: reloaded } });

    expect(getByTestId('chat-retrieval-failed')).toBeTruthy();
  });

  it('is not shown on user messages', () => {
    const { queryByTestId } = render(ChatMessage, {
      props: {
        message: assistantMessage({
          role: 'user',
          msg_metadata: { retrieval_failed: true },
        }),
      },
    });

    expect(queryByTestId('chat-retrieval-failed')).toBeNull();
  });
});
