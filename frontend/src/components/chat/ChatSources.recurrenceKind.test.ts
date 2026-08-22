/**
 * The `recurrence` citation shape (W2.5).
 *
 * A `recurrence` citation names a GROUP of items judged the same thing
 * recurring across MULTIPLE recordings — never one person's words, never a
 * single moment, and (unlike `summary`) not even anchored to a single file.
 * It needs its own badge, a "N recordings" count instead of a clock or a
 * speaker name, and a link that lands on ONE of the spanned recordings
 * rather than fabricating a moment that doesn't exist.
 */
import { describe, expect, it } from 'vitest';
import { render } from '@testing-library/svelte';

import ChatSources from './ChatSources.svelte';
import { citationHref } from '$lib/utils/chatMarkdown';
import type { ChatSource } from '$lib/types/chat';

function source(overrides: Partial<ChatSource> = {}): ChatSource {
  return {
    id: 1,
    file_uuid: '11111111-1111-1111-1111-111111111111',
    title: 'Recurring: renew the vendor contract',
    chunk_index: -1,
    digest_section: null,
    start_time: null,
    end_time: null,
    speaker: null,
    snippet: 'Came up in 3 recordings.',
    ...overrides,
  };
}

describe('ChatSources — recurrence citations (W2.5)', () => {
  it('labels a recurrence citation distinctly from summary/digest, with no speaker', () => {
    const { getByTestId, queryByTestId } = render(ChatSources, {
      props: {
        expanded: true,
        sources: [
          source({
            kind: 'recurrence',
            file_uuids: [
              '11111111-1111-1111-1111-111111111111',
              '22222222-2222-2222-2222-222222222222',
            ],
          }),
        ],
      },
    });

    expect(getByTestId('chat-source-recurrence')).toBeTruthy();
    expect(queryByTestId('chat-source-summary')).toBeNull();
    expect(queryByTestId('chat-source-digest')).toBeNull();
  });

  it('renders no clock, and shows the recording count instead', () => {
    const { container, getByTestId } = render(ChatSources, {
      props: {
        expanded: true,
        sources: [
          source({
            kind: 'recurrence',
            file_uuids: [
              '11111111-1111-1111-1111-111111111111',
              '22222222-2222-2222-2222-222222222222',
              '33333333-3333-3333-3333-333333333333',
            ],
          }),
        ],
      },
    });

    expect(container.querySelector('.source-time')).toBeNull();
    // $t() is not translation-loaded in this test environment (renders the
    // raw key — see stores/locale.ts's own doc comment on that), so this
    // asserts the count element EXISTS and the badge suppresses the clock,
    // not the interpolated text.
    expect(getByTestId('chat-source-recurrence-count')).toBeTruthy();
  });

  it('links to the FIRST recording in file_uuids, with no fabricated timestamp', () => {
    const { getByTestId } = render(ChatSources, {
      props: {
        expanded: true,
        sources: [
          source({
            kind: 'recurrence',
            file_uuid: '11111111-1111-1111-1111-111111111111',
            file_uuids: [
              '22222222-2222-2222-2222-222222222222',
              '33333333-3333-3333-3333-333333333333',
            ],
          }),
        ],
      },
    });

    const link = getByTestId('chat-source-link') as HTMLAnchorElement;
    expect(link.getAttribute('href')).toBe('/files/22222222-2222-2222-2222-222222222222');
    expect(link.getAttribute('href')).not.toContain('t=');
  });

  it('falls back to file_uuid when file_uuids is absent', () => {
    const href = citationHref({
      file_uuid: '11111111-1111-1111-1111-111111111111',
      kind: 'recurrence',
    });

    expect(href).toBe('/files/11111111-1111-1111-1111-111111111111');
  });
});
