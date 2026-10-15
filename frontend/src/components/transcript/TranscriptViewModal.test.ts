/**
 * The consolidated "view transcript" modal (issue #755) — replaces the deleted
 * `TranscriptModal.svelte` and `search/SearchTranscriptModal.svelte`.
 *
 * These pin the two things that would silently regress if the consolidation drifted:
 *  - `mode="file"` never fetches on its own — the caller already owns the data.
 *  - `mode="search"` self-fetches (issue #755 J3) and classifies segments against the
 *    server-supplied `occurrences` prop with the distinct keyword/semantic highlight classes
 *    `backend/tests/e2e/test_search.py:297` asserts.
 *  - The rendered `TranscriptSegmentList` is always read-only here (`editable` defaults to
 *    false) — a viewer-only user opening either surface must never see an edit affordance.
 */
import { describe, it, expect, vi, beforeEach } from 'vitest';
import { render, screen, waitFor } from '@testing-library/svelte';
import TranscriptViewModal from './TranscriptViewModal.svelte';
import type { SearchOccurrence } from '$stores/search';

vi.mock('$lib/axios', () => ({
  default: { get: vi.fn(), post: vi.fn(), put: vi.fn(), delete: vi.fn() },
}));

vi.mock('$stores/toast', () => ({
  toastStore: { error: vi.fn(), success: vi.fn() },
}));

vi.mock('$stores/locale', () => ({
  t: {
    subscribe: (run: (value: (key: string, opts?: Record<string, unknown>) => string) => void) => {
      run((key: string, opts?: Record<string, unknown>) =>
        opts ? `${key}:${JSON.stringify(opts)}` : key
      );
      return () => {};
    },
  },
}));

import axiosInstance from '$lib/axios';

function fileModeSegment(overrides: Record<string, unknown> = {}) {
  return {
    uuid: 'seg-1',
    start_time: 0,
    end_time: 1,
    text: 'hello world',
    ...overrides,
  };
}

function occurrence(overrides: Partial<SearchOccurrence> = {}): SearchOccurrence {
  return {
    snippet: '',
    speaker: '',
    start_time: 0,
    end_time: 1,
    chunk_index: 0,
    score: 1,
    match_type: 'content',
    has_keyword_match: true,
    highlight_type: 'keyword',
    ...overrides,
  };
}

describe('TranscriptViewModal — mode="file"', () => {
  beforeEach(() => vi.clearAllMocks());

  it('renders the already-loaded transcript without making any network call', async () => {
    render(TranscriptViewModal, {
      props: {
        isOpen: true,
        mode: 'file',
        fileName: 'meeting.mp4',
        file: {
          uuid: 'file-1',
          transcript_segments: [fileModeSegment()],
          grouped_segments: [
            {
              is_overlap_group: false,
              overlap_group_id: null,
              start_time: 0,
              end_time: 1,
              start_segment_index: 0,
              segment_uuids: ['seg-1'],
            },
          ],
        },
      },
    });

    await screen.findByText('hello world');
    expect(axiosInstance.get).not.toHaveBeenCalled();
  });

  it('renders no edit affordance — the file-mode view is read-only', async () => {
    const { container } = render(TranscriptViewModal, {
      props: {
        isOpen: true,
        mode: 'file',
        file: {
          uuid: 'file-1',
          transcript_segments: [fileModeSegment()],
          grouped_segments: [
            {
              is_overlap_group: false,
              overlap_group_id: null,
              start_time: 0,
              end_time: 1,
              start_segment_index: 0,
              segment_uuids: ['seg-1'],
            },
          ],
        },
      },
    });

    await screen.findByText('hello world');
    expect(container.querySelector('.edit-button')).toBeNull();
  });

  it("never calls the network layer for pagination — that is the page's job in file mode", async () => {
    render(TranscriptViewModal, {
      props: {
        isOpen: true,
        mode: 'file',
        file: {
          uuid: 'file-1',
          transcript_segments: [fileModeSegment()],
          grouped_segments: [
            {
              is_overlap_group: false,
              overlap_group_id: null,
              start_time: 0,
              end_time: 1,
              start_segment_index: 0,
              segment_uuids: ['seg-1'],
            },
          ],
        },
        hasMoreSegments: true,
        loadingMoreSegments: true,
      },
    });

    await screen.findByText('hello world');
    expect(axiosInstance.get).not.toHaveBeenCalled();
  });
});

describe('TranscriptViewModal — mode="search"', () => {
  beforeEach(() => {
    vi.clearAllMocks();
    vi.mocked(axiosInstance.get).mockResolvedValue({
      data: {
        transcript_segments: [
          { uuid: 'seg-1', start_time: 0, end_time: 2, text: 'find the keyword here' },
          { uuid: 'seg-2', start_time: 5, end_time: 7, text: 'an unrelated semantic match' },
        ],
        grouped_segments: [
          {
            is_overlap_group: false,
            overlap_group_id: null,
            start_time: 0,
            end_time: 2,
            start_segment_index: 0,
            segment_uuids: ['seg-1'],
          },
          {
            is_overlap_group: false,
            overlap_group_id: null,
            start_time: 5,
            end_time: 7,
            start_segment_index: 1,
            segment_uuids: ['seg-2'],
          },
        ],
        total_segments: 2,
        my_permission: 'owner',
      },
    } as never);
  });

  it('self-fetches the file when opened (issue #755 J3 — no page-level pager involved)', async () => {
    render(TranscriptViewModal, {
      props: {
        isOpen: true,
        mode: 'search',
        fileUuid: 'file-2',
        searchQuery: 'keyword',
        occurrences: [occurrence({ start_time: 0, end_time: 2, has_keyword_match: true })],
      },
    });

    await waitFor(() =>
      expect(axiosInstance.get).toHaveBeenCalledWith(
        '/files/file-2',
        expect.objectContaining({ params: expect.objectContaining({ segment_offset: 0 }) })
      )
    );
    await screen.findByText(/find the/i);
  });

  it('classifies a keyword-overlapping segment with the keyword highlight class, not a generic one', async () => {
    const { container } = render(TranscriptViewModal, {
      props: {
        isOpen: true,
        mode: 'search',
        fileUuid: 'file-2',
        searchQuery: 'keyword',
        occurrences: [occurrence({ start_time: 0, end_time: 2, has_keyword_match: true })],
      },
    });

    await waitFor(() => expect(container.querySelector('.search-keyword-match')).not.toBeNull());
    expect(container.querySelector('.search-semantic-segment')).toBeNull();
  });

  it('classifies a non-keyword occurrence as semantic, wrapping the whole segment', async () => {
    const { container } = render(TranscriptViewModal, {
      props: {
        isOpen: true,
        mode: 'search',
        fileUuid: 'file-2',
        searchQuery: 'keyword',
        occurrences: [occurrence({ start_time: 5, end_time: 7, has_keyword_match: false })],
      },
    });

    await waitFor(() => expect(container.querySelector('.search-semantic-segment')).not.toBeNull());
    expect(container.querySelector('.search-keyword-match')).toBeNull();
    expect(container.querySelector('.search-semantic-segment')?.textContent).toContain(
      'an unrelated semantic match'
    );
  });

  it('renders no edit affordance in search mode either', async () => {
    const { container } = render(TranscriptViewModal, {
      props: {
        isOpen: true,
        mode: 'search',
        fileUuid: 'file-2',
        occurrences: [],
      },
    });

    await screen.findByText(/find the/i);
    expect(container.querySelector('.edit-button')).toBeNull();
  });

  it('does not refetch when re-opened for the SAME file (guards against a fetch loop)', async () => {
    const { rerender } = render(TranscriptViewModal, {
      props: { isOpen: true, mode: 'search', fileUuid: 'file-2', occurrences: [] },
    });
    await waitFor(() => expect(axiosInstance.get).toHaveBeenCalledTimes(1));

    await rerender({ isOpen: true, mode: 'search', fileUuid: 'file-2', occurrences: [] });
    expect(axiosInstance.get).toHaveBeenCalledTimes(1);
  });

  it('refetches when re-opened for a DIFFERENT file', async () => {
    const { rerender } = render(TranscriptViewModal, {
      props: { isOpen: true, mode: 'search', fileUuid: 'file-2', occurrences: [] },
    });
    await waitFor(() => expect(axiosInstance.get).toHaveBeenCalledTimes(1));

    await rerender({ isOpen: false, mode: 'search', fileUuid: 'file-2', occurrences: [] });
    await rerender({ isOpen: true, mode: 'search', fileUuid: 'file-3', occurrences: [] });

    await waitFor(() => expect(axiosInstance.get).toHaveBeenCalledTimes(2));
    expect(axiosInstance.get).toHaveBeenLastCalledWith('/files/file-3', expect.anything());
  });
});
