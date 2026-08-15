/**
 * Regression guard for the English-only RAG notice (task #37).
 *
 * Transcription is multilingual — WhisperX handles 100+ languages and that must
 * keep working. What is English-only is the question-answering path on top of
 * it: BM25 uses an English analyzer, the default embedding model declares
 * `["en"]`, the reranker is an English MS MARCO cross-encoder, and the system
 * prompt is English prose. A non-English recording is therefore effectively
 * invisible to a question, and the model answers confidently from whatever
 * English material remains.
 *
 * That failure is silent, which is the whole reason for the notice. Mirrors
 * `ChatMessage.contextDropped.test.ts` because it is the same mechanism: the
 * server emits a `warning` frame AND persists the flag, and the component
 * renders from `msg_metadata` alone so the notice survives a reload.
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

describe('ChatMessage — unsupported-language notice (task #37)', () => {
  it('warns when the scope contained a language RAG cannot serve', () => {
    const { getByTestId } = render(ChatMessage, {
      props: {
        message: assistantMessage({
          msg_metadata: {
            unsupported_language: true,
            context_languages: { languages: ['es'], files: 2, unknown_files: 0 },
          },
        }),
      },
    });

    expect(getByTestId('chat-unsupported-language')).toBeTruthy();
  });

  it('renders regardless of how many languages are involved', () => {
    // That the notice NAMES those languages is asserted in
    // `$lib/utils/formatting.test.ts` against `formatLanguageNames`, not here:
    // this harness does not load the locale bundles, so `$t` returns the raw
    // key and no assertion on the rendered sentence can mean anything. Checking
    // the key string would pass while the copy said the opposite of the truth.
    const { getByTestId } = render(ChatMessage, {
      props: {
        message: assistantMessage({
          msg_metadata: {
            unsupported_language: true,
            context_languages: { languages: ['es', 'fr', 'ja'], files: 3, unknown_files: 1 },
          },
        }),
      },
    });

    expect(getByTestId('chat-unsupported-language')).toBeTruthy();
  });

  it('stays silent on an ordinary English answer', () => {
    // The control. Without it, a component that rendered the notice
    // unconditionally would satisfy every test above while putting a language
    // warning on every single answer in the product.
    const { queryByTestId } = render(ChatMessage, {
      props: {
        message: assistantMessage({ msg_metadata: { retrieved: 6, chunks_used: 6 } }),
      },
    });

    expect(queryByTestId('chat-unsupported-language')).toBeNull();
  });

  it('stays silent when the language is merely unknown', () => {
    // `MediaFile.language` is nullable. Undetected is NOT unsupported: firing on
    // it would put a permanent warning on every library recorded before language
    // detection existed. The backend reports `unknown_files` separately and never
    // sets the flag on that basis alone; this pins the client to the same rule.
    const { queryByTestId } = render(ChatMessage, {
      props: {
        message: assistantMessage({
          msg_metadata: {
            context_languages: { languages: [], files: 4, unknown_files: 4 },
          },
        }),
      },
    });

    expect(queryByTestId('chat-unsupported-language')).toBeNull();
  });

  it('renders the notice from persisted metadata, not from stream state', () => {
    // A reloaded thread has no `warning` frame to replay — only the DB row.
    const { getByTestId } = render(ChatMessage, {
      props: {
        message: assistantMessage({
          pending: false,
          msg_metadata: {
            unsupported_language: true,
            context_languages: { languages: ['ja'] },
          },
        }),
      },
    });

    expect(getByTestId('chat-unsupported-language')).toBeTruthy();
  });

  it('is not shown on user messages', () => {
    const { queryByTestId } = render(ChatMessage, {
      props: {
        message: assistantMessage({
          role: 'user',
          msg_metadata: { unsupported_language: true },
        }),
      },
    });

    expect(queryByTestId('chat-unsupported-language')).toBeNull();
  });
});
