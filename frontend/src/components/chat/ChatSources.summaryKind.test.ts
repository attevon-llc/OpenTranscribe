/**
 * The `summary` citation shape (issue #464).
 *
 * A `summary` citation is LLM-generated prose ABOUT the recording — a labelled
 * interpretation, never a quote, and (unlike a `digest`) not anchored to any
 * moment in the recording at all. It needs its OWN badge and its own
 * clock-less rendering, distinct from `digest`'s badge — reusing `digest`'s
 * badge text would blur two different provenances (extractive text drawn
 * verbatim from the transcript vs. LLM-generated prose about it) under one
 * label.
 */
import { describe, expect, it } from 'vitest';
import { render } from '@testing-library/svelte';

import ChatSources from './ChatSources.svelte';
import type { ChatSource } from '$lib/types/chat';

function source(overrides: Partial<ChatSource> = {}): ChatSource {
  return {
    id: 1,
    file_uuid: '11111111-1111-1111-1111-111111111111',
    title: 'Weekly sync',
    chunk_index: -1,
    digest_section: 3,
    start_time: 0,
    end_time: null,
    speaker: null,
    snippet: 'The team is on track for the migration deadline.',
    ...overrides,
  };
}

describe('ChatSources — summary citations (#464)', () => {
  it('labels a summary citation distinctly from a digest, with no speaker', () => {
    const { getByTestId, queryByTestId, queryByText } = render(ChatSources, {
      props: { expanded: true, sources: [source({ kind: 'summary' })] },
    });

    expect(getByTestId('chat-source-summary')).toBeTruthy();
    expect(queryByTestId('chat-source-digest')).toBeNull();
    expect(queryByText('Weekly sync')).toBeTruthy();
  });

  it('renders no clock / timestamp affordance for a summary citation', () => {
    const { container, getByTestId } = render(ChatSources, {
      props: {
        expanded: true,
        sources: [source({ kind: 'summary', start_time: 999 })],
      },
    });

    expect(container.querySelector('.source-time')).toBeNull();
    // The link itself must not carry a fabricated timestamp either.
    const link = getByTestId('chat-source-link') as HTMLAnchorElement;
    expect(link.getAttribute('href')).not.toContain('t=999');
  });

  it('deep-links a summary citation to the summary view, carrying its section', () => {
    const { getByTestId } = render(ChatSources, {
      props: { expanded: true, sources: [source({ kind: 'summary', digest_section: 3 })] },
    });

    const link = getByTestId('chat-source-link') as HTMLAnchorElement;
    expect(link.getAttribute('href')).toBe(
      '/files/11111111-1111-1111-1111-111111111111?view=summary&section=3'
    );
  });

  it('still shows the clock for a digest citation (unchanged, still time-anchored)', () => {
    const { container } = render(ChatSources, {
      props: { expanded: true, sources: [source({ kind: 'digest', start_time: 125.5 })] },
    });

    expect(container.querySelector('.source-time')?.textContent?.trim()).toBe('2:05');
  });

  it('still shows the clock for an ordinary chunk citation', () => {
    const { container } = render(ChatSources, {
      props: {
        expanded: true,
        sources: [source({ kind: 'chunk', speaker: 'Dana Whitfield', start_time: 12 })],
      },
    });

    expect(container.querySelector('.source-time')?.textContent?.trim()).toBe('0:12');
  });
});
