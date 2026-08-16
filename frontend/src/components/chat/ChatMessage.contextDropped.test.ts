/**
 * Regression guard for #384 — the ungrounded-answer notice.
 *
 * When retrieval finds excerpts but none fit the model's context window, the
 * server emits a `warning` frame and persists `msg_metadata.context_dropped`.
 * The user must be told: an answer produced from zero excerpts reads exactly
 * like a grounded one otherwise.
 *
 * The component renders from `msg_metadata` alone — deliberately ONE path, so
 * the notice survives a page reload rather than existing only for the lifetime
 * of the stream that produced it.
 */
import { describe, expect, it } from 'vitest';
import { render } from '@testing-library/svelte';

import ChatMessage from './ChatMessage.svelte';
import type { ChatMessage as ChatMessageType } from '$lib/types/chat';

function assistantMessage(overrides: Partial<ChatMessageType> = {}): ChatMessageType {
  return {
    uuid: 'assistant-1',
    role: 'assistant',
    content: 'The team shipped on Tuesday.',
    status: 'complete',
    ...overrides,
  };
}

describe('ChatMessage — dropped-context notice (#384)', () => {
  it('warns when every retrieved excerpt was dropped', () => {
    const { getByTestId } = render(ChatMessage, {
      props: {
        message: assistantMessage({
          msg_metadata: { retrieved: 6, chunks_used: 0, context_dropped: true },
        }),
      },
    });

    expect(getByTestId('chat-context-dropped')).toBeTruthy();
  });

  it('stays silent on an ordinary grounded answer', () => {
    const { queryByTestId } = render(ChatMessage, {
      props: {
        message: assistantMessage({ msg_metadata: { retrieved: 6, chunks_used: 6 } }),
      },
    });

    expect(queryByTestId('chat-context-dropped')).toBeNull();
  });

  it('renders the notice from persisted metadata, not from stream state', () => {
    // A reloaded thread has no `warning` frame to replay — only the DB row. The
    // notice must still appear, or it silently disappears on refresh.
    const reloaded = assistantMessage({
      pending: false,
      msg_metadata: { context_dropped: true },
    });
    const { getByTestId } = render(ChatMessage, { props: { message: reloaded } });

    expect(getByTestId('chat-context-dropped')).toBeTruthy();
  });

  it('is not shown on user messages', () => {
    const { queryByTestId } = render(ChatMessage, {
      props: {
        message: assistantMessage({
          role: 'user',
          msg_metadata: { context_dropped: true },
        }),
      },
    });

    expect(queryByTestId('chat-context-dropped')).toBeNull();
  });
});

describe('ChatMessage — nothing-was-searched notice (#438)', () => {
  it('warns when no excerpt reached the model at all', () => {
    const { getByTestId } = render(ChatMessage, {
      props: {
        message: assistantMessage({
          content: 'I do not have enough information in the provided excerpts.',
          msg_metadata: { retrieved: 0, chunks_used: 0, files_searched: 'all', no_context: true },
        }),
      },
    });

    expect(getByTestId('chat-no-context')).toBeTruthy();
  });

  it('stays silent on a grounded answer', () => {
    const { queryByTestId } = render(ChatMessage, {
      props: {
        message: assistantMessage({
          msg_metadata: { retrieved: 12, chunks_used: 8, files_searched: 'all' },
        }),
      },
    });

    expect(queryByTestId('chat-no-context')).toBeNull();
  });

  it('shows the dropped-context notice instead when excerpts WERE retrieved', () => {
    // Both flags can only disagree if something upstream is wrong, but the
    // rendering must still name one defect rather than stacking two notices.
    const { getByTestId, queryByTestId } = render(ChatMessage, {
      props: {
        message: assistantMessage({
          msg_metadata: { retrieved: 6, chunks_used: 0, context_dropped: true },
        }),
      },
    });

    expect(getByTestId('chat-context-dropped')).toBeTruthy();
    expect(queryByTestId('chat-no-context')).toBeNull();
  });

  it('renders from persisted metadata, so it survives a reload', () => {
    const { getByTestId } = render(ChatMessage, {
      props: { message: assistantMessage({ pending: false, msg_metadata: { no_context: true } }) },
    });

    expect(getByTestId('chat-no-context')).toBeTruthy();
  });
});
