/**
 * The digest citation shape (#403 Stage 4, addendum G7).
 *
 * A `digest` citation is DERIVED text — an extractive summary of a span of the
 * recording — not something a participant said. Rendering it identically to a
 * transcript chunk attributes words to a person who never spoke them, which is
 * the same silent-wrong-answer class as #385: nothing on screen looks broken.
 *
 * The back-compatibility default is the load-bearing case and it is easy to get
 * backwards. Every citation persisted before Stage 4 carries **no** `kind`, so
 * an absent value must mean `chunk`. Defaulting the other way would relabel
 * every historic citation in the product as a summary.
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
    chunk_index: 3,
    start_time: 125.5,
    end_time: 160,
    speaker: 'Dana Whitfield',
    snippet: 'We agreed the budget.',
    ...overrides,
  };
}

describe('ChatSources — digest citations (G7)', () => {
  it('labels a digest as a summary instead of attributing it to a speaker', () => {
    const { getByTestId, queryByText } = render(ChatSources, {
      props: {
        expanded: true,
        sources: [source({ kind: 'digest', digest_section: 0, speaker: null })],
      },
    });

    expect(getByTestId('chat-source-digest')).toBeTruthy();
    expect(queryByText('Dana Whitfield')).toBeNull();
  });

  it('renders an ordinary chunk with its speaker and no summary badge', () => {
    const { queryByTestId, getByText } = render(ChatSources, {
      props: { expanded: true, sources: [source({ kind: 'chunk' })] },
    });

    expect(queryByTestId('chat-source-digest')).toBeNull();
    expect(getByText('Dana Whitfield')).toBeTruthy();
  });

  it('treats a citation with NO kind as a chunk, not a summary', () => {
    // Every citation persisted before Stage 4 looks like this. Getting the
    // default backwards relabels the whole history as summaries.
    const legacy = source();
    delete (legacy as Partial<ChatSource>).kind;

    const { queryByTestId, getByText } = render(ChatSources, {
      props: { expanded: true, sources: [legacy] },
    });

    expect(queryByTestId('chat-source-digest')).toBeNull();
    expect(getByText('Dana Whitfield')).toBeTruthy();
  });

  it('shows the expand toggle for a wide (700-char) digest snippet in the same list as an ordinary chunk (#913)', () => {
    // #832 gave digests their own wider snippet cap (DIGEST_SNIPPET_CHARS, 700
    // today) so a digest citation commonly carries far more text than an
    // ordinary chunk's 240-char cap ever did. One ChatSources list must handle
    // both sizes: the digest card gets a toggle, the short chunk card does not.
    const wideDigest = 'word '.repeat(140).trim(); // ~700 chars
    const { getAllByTestId, queryByTestId } = render(ChatSources, {
      props: {
        expanded: true,
        sources: [
          source({ id: 1, kind: 'digest', digest_section: 0, speaker: null, snippet: wideDigest }),
          source({ id: 2, kind: 'chunk' }),
        ],
      },
    });

    const toggles = getAllByTestId('chat-source-snippet-toggle');
    expect(toggles).toHaveLength(1);
    expect(queryByTestId('chat-source-recurrence')).toBeNull();
  });

  it('still deep-links a digest to the section timestamp, not to 0:00', () => {
    // A digest indexed with start_time=0 would give every summary citation a
    // link that looks like it works and lands at the top of the recording.
    const { getByTestId } = render(ChatSources, {
      props: {
        expanded: true,
        sources: [source({ kind: 'digest', digest_section: 2, speaker: null, start_time: 125.5 })],
      },
    });

    // citationHref emits whole seconds; the point is that it is the SECTION's
    // time and not the file's start, so both halves are asserted.
    const link = getByTestId('chat-source-link') as HTMLAnchorElement;
    expect(link.getAttribute('href')).toContain('t=125');
    expect(link.getAttribute('href')).not.toContain('t=0');
  });
});
