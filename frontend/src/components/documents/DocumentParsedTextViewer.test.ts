/**
 * DocumentParsedTextViewer — search-within-document (v400, #362 lane C5).
 *
 * Reuses `$lib/utils/searchHighlight` (the same utility TranscriptModal/SummaryModal
 * highlight with) rather than a second highlighter. These tests pin: matches are
 * counted across ALL chunks (not just the first), `matchesChanged` reports the
 * total, and exactly one span across the whole document — the one at
 * `currentMatchIndex` — carries the `current` class, even when it is in a LATER
 * chunk than the first match.
 */
import { describe, it, expect, vi } from 'vitest';
import { render, screen } from '@testing-library/svelte';

vi.mock('$stores/locale', () => ({
  t: {
    subscribe: (run: (value: (key: string) => string) => void) => {
      run((key: string) => key);
      return () => {};
    },
  },
}));

import DocumentParsedTextViewer from './DocumentParsedTextViewer.svelte';

const CHUNKS = [
  {
    chunk_index: 0,
    text: 'The quick brown fox.',
    char_start: 0,
    char_end: 21,
    page: null,
    section_path: [],
    block_types: [],
  },
  {
    chunk_index: 1,
    text: 'Another fox appears here.',
    char_start: 21,
    char_end: 47,
    page: null,
    section_path: [],
    block_types: [],
  },
];

describe('DocumentParsedTextViewer — search-within-document', () => {
  it('renders plain chunk text when there is no search query', () => {
    render(DocumentParsedTextViewer, { props: { chunks: CHUNKS, searchQuery: '' } });

    expect(screen.getByText('The quick brown fox.')).toBeInTheDocument();
  });

  it('reports the total match count across every chunk via matchesChanged', () => {
    const handler = vi.fn();
    render(DocumentParsedTextViewer, {
      props: { chunks: CHUNKS, searchQuery: 'fox' },
      events: { matchesChanged: handler },
    });

    expect(handler).toHaveBeenCalled();
    const last = handler.mock.calls[handler.mock.calls.length - 1][0];
    expect(last.detail).toEqual({ total: 2 });
  });

  it('marks exactly one match current, in the chunk currentMatchIndex points to', () => {
    const { container } = render(DocumentParsedTextViewer, {
      props: { chunks: CHUNKS, searchQuery: 'fox', currentMatchIndex: 1 },
    });

    const current = container.querySelectorAll('.transcript-search-highlight.current');
    expect(current).toHaveLength(1);
    // The second "fox" occurrence lives in chunk_index 1's text.
    expect(current[0].closest('[data-chunk-index]')?.getAttribute('data-chunk-index')).toBe('1');
  });

  it('is case-insensitive', () => {
    const { container } = render(DocumentParsedTextViewer, {
      props: { chunks: CHUNKS, searchQuery: 'FOX' },
    });

    expect(container.querySelectorAll('.transcript-search-highlight')).toHaveLength(2);
  });
});
