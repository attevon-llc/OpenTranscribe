/**
 * G1: `navigateToMatch`'s "load more until the target resolves" loop
 * (`while (resolvedIndex === -1 && hasMoreSegments) { await loadMoreSegments(); ... }`)
 * had no bound. `loadMoreSegments`'s catch clause only `console.error`d — it made
 * zero assignments — so a rejected page request left `hasMoreSegments` true and
 * the loop re-issued the identical request forever, with the spinner stuck (the
 * `navigatingToMatch = false` after the loop never ran) and no visible error.
 *
 * This drives the real component (no vitest file previously existed for it —
 * see plan item J7): mount with one occurrence whose time range is NOT in the
 * first page of segments, so `nextMatch()` must page forward to resolve it.
 */
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';
import { fireEvent, render, waitFor } from '@testing-library/svelte';

vi.mock('$lib/axios', () => {
  const axiosInstance = { get: vi.fn(), post: vi.fn(), put: vi.fn(), delete: vi.fn() };
  return { default: axiosInstance, isRequestCancelled: () => false };
});

vi.mock('$stores/locale', async () => {
  const { readable } = await import('svelte/store');
  const en = (await import('$lib/i18n/locales/en.json')).default as Record<string, string>;
  return { t: readable((key: string) => en[key] ?? key) };
});

const { toastError } = vi.hoisted(() => ({ toastError: vi.fn() }));
vi.mock('$stores/toast', () => ({
  toastStore: { error: toastError, show: vi.fn(), dismiss: vi.fn(), clear: vi.fn() },
}));

import axiosInstance from '$lib/axios';
import SearchTranscriptModal from './SearchTranscriptModal.svelte';

const get = vi.mocked(axiosInstance.get);

function firstPageResponse() {
  return {
    data: {
      transcript_segments: [{ uuid: 's1', start_time: 0, end_time: 2, text: 'hello there' }],
      total_segments: 999,
      my_permission: 'owner',
    },
  };
}

const occurrence = {
  snippet: 'far away match',
  speaker: 'Someone',
  start_time: 5000,
  end_time: 5001,
  chunk_index: 0,
  score: 1,
  match_type: 'content' as const,
  has_keyword_match: true,
  highlight_type: 'keyword' as const,
};

beforeEach(() => {
  get.mockReset();
  toastError.mockReset();
});

afterEach(() => {
  vi.restoreAllMocks();
});

describe('SearchTranscriptModal — next-match paging', () => {
  it('a rejected page request stops the loop after one retry and surfaces an error', async () => {
    get.mockResolvedValueOnce(firstPageResponse()); // initial loadTranscript()
    get.mockRejectedValueOnce(new Error('network down')); // the one loadMoreSegments() call

    const { getAllByTitle } = render(SearchTranscriptModal, {
      props: {
        isOpen: true,
        fileUuid: 'file-1',
        fileName: 'f.wav',
        searchQuery: 'match',
        occurrences: [occurrence],
      },
    });

    await waitFor(() => expect(get).toHaveBeenCalledTimes(1));

    const nextBtn = getAllByTitle('Next match (Enter)')[0];
    await fireEvent.click(nextBtn);

    await waitFor(() => expect(toastError).toHaveBeenCalledTimes(1));

    // The loop must not have re-issued the request after the rejection: exactly
    // one initial load + one failed page request, never a third call.
    expect(get).toHaveBeenCalledTimes(2);
  });

  it('control: a healthy backend pages until the match resolves (never zero retries)', async () => {
    get.mockResolvedValueOnce(firstPageResponse()); // initial loadTranscript()
    get.mockResolvedValueOnce({
      data: {
        transcript_segments: [
          { uuid: 's2', start_time: 5000, end_time: 5002, text: 'far away match' },
        ],
        total_segments: 999,
      },
    }); // the page that contains the target match

    const { getAllByTitle } = render(SearchTranscriptModal, {
      props: {
        isOpen: true,
        fileUuid: 'file-1',
        fileName: 'f.wav',
        searchQuery: 'match',
        occurrences: [occurrence],
      },
    });

    await waitFor(() => expect(get).toHaveBeenCalledTimes(1));

    const nextBtn = getAllByTitle('Next match (Enter)')[0];
    await fireEvent.click(nextBtn);

    await waitFor(() => expect(get).toHaveBeenCalledTimes(2));
    expect(toastError).not.toHaveBeenCalled();
  });
});
