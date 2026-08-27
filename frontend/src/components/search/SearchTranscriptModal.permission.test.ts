/**
 * `myPermission` used a `null` default, indistinguishable from "confirmed
 * unshared/owner" — so `canViewOriginal` (gating the redaction "show
 * original" toggle) could read true for a split second while the real
 * permission was still in flight, before flipping to false for a
 * non-owner. Mirrors the same sentinel fix already applied to
 * `routes/files/[id]/+page.svelte`: `myPermission` starts `undefined`,
 * and `canViewOriginal` requires `permissionLoaded` before ever reading true.
 *
 * These drive the real component: hold the `/files/{uuid}` fetch pending to
 * observe the pre-resolution state, then resolve it to observe the gated
 * outcome.
 */
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';
import { render, waitFor } from '@testing-library/svelte';

vi.mock('$lib/axios', () => {
  const axiosInstance = { get: vi.fn(), post: vi.fn(), put: vi.fn(), delete: vi.fn() };
  return { default: axiosInstance, isRequestCancelled: () => false };
});

vi.mock('$stores/locale', async () => {
  const { readable } = await import('svelte/store');
  const en = (await import('$lib/i18n/locales/en.json')).default as Record<string, string>;
  return { t: readable((key: string) => en[key] ?? key) };
});

vi.mock('$stores/toast', () => ({
  toastStore: { error: vi.fn(), show: vi.fn(), dismiss: vi.fn(), clear: vi.fn() },
}));

import axiosInstance from '$lib/axios';
import SearchTranscriptModal from './SearchTranscriptModal.svelte';

const get = vi.mocked(axiosInstance.get);

const occurrence = {
  snippet: 'a match',
  speaker: 'Someone',
  start_time: 0,
  end_time: 1,
  chunk_index: 0,
  score: 1,
  match_type: 'content' as const,
  has_keyword_match: true,
  highlight_type: 'keyword' as const,
};

function redactedSegmentResponse(myPermission: string | null) {
  return {
    data: {
      transcript_segments: [
        {
          uuid: 's1',
          start_time: 0,
          end_time: 2,
          text: 'hello there',
          redactions: [{ start: 0, end: 5, category: 'pii' }],
        },
      ],
      total_segments: 1,
      my_permission: myPermission,
    },
  };
}

beforeEach(() => {
  get.mockReset();
});

afterEach(() => {
  vi.restoreAllMocks();
});

function renderModal() {
  return render(SearchTranscriptModal, {
    props: {
      isOpen: true,
      fileUuid: 'file-1',
      fileName: 'f.wav',
      searchQuery: 'match',
      occurrences: [occurrence],
    },
  });
}

describe('SearchTranscriptModal — permission-gated redaction toggle', () => {
  it('the "show original" toggle is absent while the permission fetch is still in flight', async () => {
    let resolveFetch: (v: unknown) => void;
    get.mockReturnValueOnce(
      new Promise((resolve) => {
        resolveFetch = resolve;
      })
    );

    const { queryByText } = renderModal();

    // The fetch is pending — myPermission is still `undefined`, so
    // canViewOriginal must be false regardless of what the eventual
    // permission turns out to be.
    expect(queryByText('Show original')).toBeNull();

    resolveFetch!(redactedSegmentResponse('owner'));
    await waitFor(() => expect(queryByText('Show original')).not.toBeNull());
  });

  it('shows the toggle once resolved for owner permission', async () => {
    get.mockResolvedValueOnce(redactedSegmentResponse('owner'));

    const { queryByText } = renderModal();

    await waitFor(() => expect(queryByText('Show original')).not.toBeNull());
  });

  it('shows the toggle once resolved for legacy-unshared (null) permission', async () => {
    get.mockResolvedValueOnce(redactedSegmentResponse(null));

    const { queryByText } = renderModal();

    await waitFor(() => expect(queryByText('Show original')).not.toBeNull());
  });

  it('keeps the toggle hidden once resolved for a real non-owner permission (viewer)', async () => {
    get.mockResolvedValueOnce(redactedSegmentResponse('viewer'));

    const { queryByText, findByText } = renderModal();

    // Wait for loading to settle on something we know renders post-fetch,
    // then assert the toggle never appeared.
    await findByText('hello there', { exact: false }).catch(() => undefined);
    await waitFor(() => expect(get).toHaveBeenCalledTimes(1));
    expect(queryByText('Show original')).toBeNull();
  });

  it('keeps the toggle hidden once resolved for a real non-owner permission (editor)', async () => {
    get.mockResolvedValueOnce(redactedSegmentResponse('editor'));

    const { queryByText } = renderModal();

    await waitFor(() => expect(get).toHaveBeenCalledTimes(1));
    expect(queryByText('Show original')).toBeNull();
  });
});
