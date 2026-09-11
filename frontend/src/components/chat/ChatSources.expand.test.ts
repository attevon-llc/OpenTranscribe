/**
 * The per-card "show more / show less" snippet toggle (issue #913).
 *
 * Raising the backend's chunk snippet cap (`citations.py::SNIPPET_CHARS`) has zero
 * reader-visible effect while the card clamps to 2 lines regardless of snippet
 * length — the extra text is generated and then clipped by CSS. This is the UI half
 * that makes a wider snippet actually readable: a toggle, rendered only when there is
 * more to show, that expands the card in place.
 *
 * Identity translator (same pattern as ChatReasoning.test.ts): returns the raw
 * dot-notation key so assertions can pin which key the component picked, without
 * booting real i18next + locale JSON in the test environment.
 */
import { describe, expect, it } from 'vitest';
import { render, fireEvent, within } from '@testing-library/svelte';

import ChatSources from './ChatSources.svelte';
import type { ChatSource } from '$lib/types/chat';

function source(overrides: Partial<ChatSource> = {}): ChatSource {
  return {
    id: 1,
    file_uuid: '11111111-1111-1111-1111-111111111111',
    title: 'Weekly sync',
    chunk_index: 3,
    start_time: 125.5,
    end_time: 160,
    speaker: 'Dana Whitfield',
    snippet: 'We agreed the budget.',
    ...overrides,
  };
}

// The card displayed up to 240 chars in full before this issue (SNIPPET_CHARS).
// The boundary below pins that "no citation that exists today gains a button
// that reveals nothing" — see ChatSources.svelte's SNIPPET_COLLAPSE_CHARS comment.
const AT_BOUNDARY = 'x'.repeat(240);
const OVER_BOUNDARY = 'x'.repeat(241);

describe('ChatSources — per-card snippet expand/collapse (#913)', () => {
  it('renders no toggle for a snippet at exactly the collapse boundary', () => {
    const { queryByTestId } = render(ChatSources, {
      props: { expanded: true, sources: [source({ snippet: AT_BOUNDARY })] },
    });

    expect(queryByTestId('chat-source-snippet-toggle')).toBeNull();
  });

  it('renders a collapsed toggle for a snippet one char over the boundary', () => {
    const { getByTestId } = render(ChatSources, {
      props: { expanded: true, sources: [source({ snippet: OVER_BOUNDARY })] },
    });

    const toggle = getByTestId('chat-source-snippet-toggle');
    expect(toggle.getAttribute('aria-expanded')).toBe('false');
    expect(getByTestId('chat-source-snippet').className).toContain('clamped');
  });

  it('flips expanded state on click, and flips back on a second click', async () => {
    const { getByTestId } = render(ChatSources, {
      props: { expanded: true, sources: [source({ snippet: OVER_BOUNDARY })] },
    });

    const toggle = getByTestId('chat-source-snippet-toggle');
    const snippet = getByTestId('chat-source-snippet');

    await fireEvent.click(toggle);
    expect(toggle.getAttribute('aria-expanded')).toBe('true');
    expect(snippet.className).not.toContain('clamped');

    await fireEvent.click(toggle);
    expect(toggle.getAttribute('aria-expanded')).toBe('false');
    expect(snippet.className).toContain('clamped');
  });

  it('labels the toggle with the show-more/show-less keys', async () => {
    const { getByTestId } = render(ChatSources, {
      props: { expanded: true, sources: [source({ snippet: OVER_BOUNDARY })] },
    });

    const toggle = getByTestId('chat-source-snippet-toggle');
    expect(toggle.textContent?.trim()).toBe('chat.sources.showMore');

    await fireEvent.click(toggle);
    expect(toggle.textContent?.trim()).toBe('chat.sources.showLess');
  });

  it('gives aria-controls a globally unique id across multiple ChatSources instances', () => {
    const first = render(ChatSources, {
      props: { expanded: true, sources: [source({ id: 1, snippet: OVER_BOUNDARY })] },
    });
    const second = render(ChatSources, {
      props: { expanded: true, sources: [source({ id: 1, snippet: OVER_BOUNDARY })] },
    });

    const firstToggle = within(first.container).getByTestId('chat-source-snippet-toggle');
    const secondToggle = within(second.container).getByTestId('chat-source-snippet-toggle');
    const firstControls = firstToggle.getAttribute('aria-controls');
    const secondControls = secondToggle.getAttribute('aria-controls');

    expect(firstControls).toBeTruthy();
    expect(secondControls).toBeTruthy();
    expect(firstControls).not.toBe(secondControls);

    // aria-controls must resolve to a real element in each instance's own DOM.
    expect(first.container.querySelector(`#${firstControls}`)).toBeTruthy();
    expect(second.container.querySelector(`#${secondControls}`)).toBeTruthy();

    first.unmount();
    second.unmount();
  });

  it('expanding card A leaves card B collapsed (no shared boolean)', async () => {
    const { getAllByTestId } = render(ChatSources, {
      props: {
        expanded: true,
        sources: [
          source({ id: 1, snippet: OVER_BOUNDARY }),
          source({ id: 2, snippet: OVER_BOUNDARY }),
        ],
      },
    });

    const [toggleA, toggleB] = getAllByTestId('chat-source-snippet-toggle');
    await fireEvent.click(toggleA);

    expect(toggleA.getAttribute('aria-expanded')).toBe('true');
    expect(toggleB.getAttribute('aria-expanded')).toBe('false');
  });

  it('is never a descendant of the citation link anchor', () => {
    const { getByTestId } = render(ChatSources, {
      props: { expanded: true, sources: [source({ snippet: OVER_BOUNDARY })] },
    });

    const link = getByTestId('chat-source-link');
    const toggle = getByTestId('chat-source-snippet-toggle');

    expect(link.contains(toggle)).toBe(false);
  });
});
