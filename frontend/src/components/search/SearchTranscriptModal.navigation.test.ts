/**
 * G1: `navigateToMatch`'s "load more until the target resolves" loop
 * (`while (resolvedIndex === -1 && hasMoreSegments) { await loadMoreSegments(); ... }`)
 * had no bound. `loadMoreSegments`'s catch clause only `console.error`d — it made
 * zero assignments — so a rejected page request left `hasMoreSegments` true and
 * the loop re-issued the identical request forever, with the spinner stuck (the
 * `navigatingToMatch = false` after the loop never ran) and no visible error.
 *
 * G1 FOLLOW-UP (adversarial review): the original fix went too far the other
 * way — clearing `hasMoreSegments` on the FIRST failure permanently truncated
 * the transcript, even for a purely transient hiccup (one dropped connection,
 * one backend restart), with the same user-visible effect as never fixing the
 * hang: a search result you can never scroll past. `loadMoreSegments` now
 * retries up to `MAX_LOAD_MORE_RETRIES` times with the shared jittered backoff
 * (`$lib/utils/backoff.ts`, the same one `uploadService.ts` uses) before
 * genuinely giving up.
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

// Upper bounds of the shared `reconnectDelayMs(attempt)` backoff (equal
// jitter over `[2**attempt * 500, 2**attempt * 1000)`) — advancing fake timers
// by the max keeps this deterministic regardless of the jittered draw.
const RETRY_DELAY_FOR_ATTEMPT_1 = 2000;
const RETRY_DELAY_FOR_ATTEMPT_2 = 4000;
const RETRY_DELAY_FOR_ATTEMPT_3 = 8000;

describe('SearchTranscriptModal — next-match paging', () => {
  it('a single transient failure retries and resolves the match — the list is NOT truncated', async () => {
    get.mockResolvedValueOnce(firstPageResponse()); // initial loadTranscript()
    get.mockRejectedValueOnce(new Error('one dropped connection')); // first loadMoreSegments() attempt
    get.mockResolvedValueOnce({
      data: {
        transcript_segments: [
          { uuid: 's2', start_time: 5000, end_time: 5002, text: 'far away match' },
        ],
        total_segments: 999,
      },
    }); // the retry succeeds and lands the page containing the target match

    vi.useFakeTimers();
    try {
      const { getAllByTitle } = render(SearchTranscriptModal, {
        props: {
          isOpen: true,
          fileUuid: 'file-1',
          fileName: 'f.wav',
          searchQuery: 'match',
          occurrences: [occurrence],
        },
      });

      // Under fake timers, vi.waitFor's condition becomes true the instant
      // the mock is CALLED (synchronously, before its promise resolves) — it
      // does not itself yield real time for the pending promise's `.then()`
      // continuation to run. Flush that explicitly, or the initial fetch's
      // response (which sets `hasMoreSegments`) has not landed yet by the time
      // we click, and `nextMatch()`'s while loop never starts.
      await vi.advanceTimersByTimeAsync(0);
      expect(get).toHaveBeenCalledTimes(1);

      const nextBtn = getAllByTitle('Next match (Enter)')[0];
      await fireEvent.click(nextBtn);

      // Let the first (failing) attempt's rejection flush, then advance past
      // its backoff so the retry fires. `advanceTimersByTimeAsync` (unlike
      // `vi.waitFor`'s own polling, which schedules its retries on the now-fake
      // clock and never observes them) flushes pending microtasks on every
      // step, including at a 0ms advance.
      await vi.advanceTimersByTimeAsync(0);
      expect(get).toHaveBeenCalledTimes(2);
      await vi.advanceTimersByTimeAsync(RETRY_DELAY_FOR_ATTEMPT_1);

      expect(get).toHaveBeenCalledTimes(3);
      // The retry succeeded — no error, no permanent truncation.
      expect(toastError).not.toHaveBeenCalled();
    } finally {
      vi.useRealTimers();
    }
  });

  it('exhausts MAX_LOAD_MORE_RETRIES on a persistent failure, then genuinely gives up', async () => {
    get.mockResolvedValueOnce(firstPageResponse()); // initial loadTranscript()
    // 1 initial attempt + 3 retries, all rejected.
    get.mockRejectedValueOnce(new Error('network down'));
    get.mockRejectedValueOnce(new Error('network down'));
    get.mockRejectedValueOnce(new Error('network down'));
    get.mockRejectedValueOnce(new Error('network down'));

    vi.useFakeTimers();
    try {
      const { getAllByTitle } = render(SearchTranscriptModal, {
        props: {
          isOpen: true,
          fileUuid: 'file-1',
          fileName: 'f.wav',
          searchQuery: 'match',
          occurrences: [occurrence],
        },
      });

      // See the sibling test above for why this flush (not vi.waitFor alone)
      // is needed under fake timers.
      await vi.advanceTimersByTimeAsync(0);
      expect(get).toHaveBeenCalledTimes(1);

      const nextBtn = getAllByTitle('Next match (Enter)')[0];
      await fireEvent.click(nextBtn);

      await vi.advanceTimersByTimeAsync(0);
      expect(get).toHaveBeenCalledTimes(2);
      await vi.advanceTimersByTimeAsync(RETRY_DELAY_FOR_ATTEMPT_1);
      expect(get).toHaveBeenCalledTimes(3);
      await vi.advanceTimersByTimeAsync(RETRY_DELAY_FOR_ATTEMPT_2);
      expect(get).toHaveBeenCalledTimes(4);
      await vi.advanceTimersByTimeAsync(RETRY_DELAY_FOR_ATTEMPT_3);
      expect(get).toHaveBeenCalledTimes(5);

      expect(toastError).toHaveBeenCalledTimes(1);

      // Retries are bounded: no sixth call after giving up.
      expect(get).toHaveBeenCalledTimes(5);
    } finally {
      vi.useRealTimers();
    }
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
