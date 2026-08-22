/**
 * The disambiguation chip's event has to survive TWO forwarding hops to reach
 * the page: ChatMessageMeta -> ChatMessage -> ChatThread. Per this folder's
 * CLAUDE.md ("Events must be forwarded at EVERY hop"), a `dispatch(...)` in a
 * child component reaches the store only if every intermediate component
 * re-emits it — Svelte does not bubble component events on its own.
 *
 * ChatMessageMeta.test.ts already proves the first hop (chip click ->
 * ChatMessageMeta dispatches `disambiguate`). This file proves the other two:
 * ChatMessage was rendering `<ChatMessageMeta {message} />` with no listener
 * at all, so the pick reached nowhere; ChatThread forwards messages events
 * generically but had never been given a `disambiguate` case to forward.
 */
import { beforeAll, describe, expect, it } from 'vitest';
import { render } from '@testing-library/svelte';

import ChatThreadDisambiguateTestHost from './ChatThreadDisambiguateTestHost.svelte';
import type { ChatMessage as ChatMessageType } from '$lib/types/chat';

// jsdom implements no scroll layout at all, so `Element.scrollTo` does not
// exist — `ChatThread`'s auto-scroll-to-bottom effect calls it on every
// render. Nothing here exercises scroll behavior, only event forwarding.
beforeAll(() => {
  Element.prototype.scrollTo =
    Element.prototype.scrollTo || ((() => {}) as typeof Element.prototype.scrollTo);
});

function assistantMessage(overrides: Partial<ChatMessageType> = {}): ChatMessageType {
  return {
    uuid: 'assistant-1',
    role: 'assistant',
    content: 'The team shipped on Tuesday.',
    status: 'complete',
    ...overrides,
  };
}

async function expandPanel(getByTestId: (id: string) => HTMLElement) {
  const toggle = getByTestId('chat-meta-toggle');
  toggle.dispatchEvent(new MouseEvent('click', { bubbles: true }));
  await new Promise((r) => setTimeout(r, 0));
}

describe('ChatThread — disambiguation chip forwarding', () => {
  it('a chip click inside a rendered thread reaches the ChatThread consumer', async () => {
    let picked: string | undefined;
    const { getByTestId, getAllByTestId } = render(ChatThreadDisambiguateTestHost, {
      props: {
        messages: [
          assistantMessage({
            msg_metadata: { speaker_resolution: { matched: [], ambiguous: ['Alice'] } },
          }),
        ],
        onDisambiguate: (name: string) => {
          picked = name;
        },
      },
    });

    await expandPanel(getByTestId);
    getAllByTestId('chat-speaker-disambiguation-chip')[0].dispatchEvent(
      new MouseEvent('click', { bubbles: true })
    );

    expect(picked).toBe('Alice');
  });

  it('picking one of several candidates forwards the exact name clicked', async () => {
    let picked: string | undefined;
    const { getByTestId, getAllByTestId } = render(ChatThreadDisambiguateTestHost, {
      props: {
        messages: [
          assistantMessage({
            msg_metadata: { speaker_resolution: { matched: [], ambiguous: ['Alice', 'Alex'] } },
          }),
        ],
        onDisambiguate: (name: string) => {
          picked = name;
        },
      },
    });

    await expandPanel(getByTestId);
    getAllByTestId('chat-speaker-disambiguation-chip')[1].dispatchEvent(
      new MouseEvent('click', { bubbles: true })
    );

    expect(picked).toBe('Alex');
  });

  it('a thread with no ambiguous candidates forwards nothing', async () => {
    let picked: string | undefined;
    const { queryByTestId } = render(ChatThreadDisambiguateTestHost, {
      props: {
        messages: [
          assistantMessage({
            msg_metadata: { speaker_resolution: { matched: ['Dana'], ambiguous: [] } },
          }),
        ],
        onDisambiguate: (name: string) => {
          picked = name;
        },
      },
    });

    expect(queryByTestId('chat-speaker-disambiguation-chip')).toBeNull();
    expect(picked).toBeUndefined();
  });
});
