/**
 * `files/[id]/+page.svelte` is the file-detail route: it owns the ONLY
 * `GET /files/{id}` call for this page (`fetchFileDetails`, called from
 * `onMount`), and its loading/error/success states are entirely driven by
 * that single request. Real behavior found while reading the component:
 *
 * - `isLoading` starts `true` and gates a `<FileDetailSkeleton />`.
 * - `fetchFileDetails` has ONE catch-all `catch (error)` block. A 404, a 403,
 *   and a network failure all land there and all set the SAME generic
 *   `pageErrorMessage` ("Failed to load file details. Please try again.")
 *   with a retry button — there is no `error.status` branch at all in this
 *   function (`getErrorStatus` is only used later, inside speaker-save and
 *   segment-save error handling, not here). So a 403 on someone else's file
 *   cannot leak content: on any failure `file` is simply never assigned, and
 *   the template's `{:else if file}` branch (which renders `FileHeader`,
 *   `TranscriptDisplay`, etc.) never executes.
 * - On success, `file` is assigned from `response.data` and the page's own
 *   `<svelte:head><title>{file?.filename || ...}</title>` updates — that
 *   title element is owned directly by this file, not a child component, so
 *   it is a reliable success signal without needing the ~15 heavy child
 *   components (FileHeader, VideoPlayer, TranscriptDisplay, ...) to fully
 *   render against their own real dependencies. Those children are stubbed
 *   out below (this suite scopes to the page's own fetch/loading/error state
 *   machine, not to every child panel — each of those is/should be tested
 *   independently, per the same rationale `SettingsModal.test.ts` documents
 *   for its own child settings panels).
 */
import { describe, it, expect, vi, beforeEach } from 'vitest';
import { render, waitFor } from '@testing-library/svelte';

const mockAxios = vi.hoisted(() => ({
  get: vi.fn(),
  post: vi.fn(),
  put: vi.fn(),
  delete: vi.fn(),
}));
vi.mock('$lib/axios', () => ({ default: mockAxios, isRequestCancelled: () => false }));

vi.mock('$stores/locale', () => ({
  t: {
    subscribe: (run: (value: (key: string) => string) => void) => {
      run((key: string) => key);
      return () => {};
    },
  },
}));

const mockGetAISuggestions = vi.hoisted(() => vi.fn());
vi.mock('$lib/api/suggestions', () => ({ getAISuggestions: mockGetAISuggestions }));

const mockGetMediaStreamUrl = vi.hoisted(() => vi.fn());
vi.mock('$lib/api/mediaUrl', () => ({
  getMediaStreamUrl: mockGetMediaStreamUrl,
  getCachedUrlInfo: () => null,
  createUrlRefresher: () => ({ stop: vi.fn() }),
  clearMediaUrlCache: vi.fn(),
}));

// Every child panel this route composes is stubbed to a no-op: this suite
// scopes to the page's own fetch/loading/error state machine (see file
// header comment), not to whether each child renders correctly on real data.
function noopComponent() {
  return () => {};
}
vi.mock('$components/VideoPlayer.svelte', () => ({ default: noopComponent() }));
vi.mock('$components/WaveformPlayer.svelte', () => ({ default: noopComponent() }));
vi.mock('$components/MetadataDisplay.svelte', () => ({ default: noopComponent() }));
vi.mock('$components/AnalyticsSection.svelte', () => ({ default: noopComponent() }));
vi.mock('$components/TranscriptDisplay.svelte', () => ({ default: noopComponent() }));
vi.mock('$components/FileHeader.svelte', () => ({ default: noopComponent() }));
vi.mock('$components/TagsSection.svelte', () => ({ default: noopComponent() }));
vi.mock('$components/CommentSection.svelte', () => ({ default: noopComponent() }));
vi.mock('$components/CollectionsSection.svelte', () => ({ default: noopComponent() }));
vi.mock('$components/SelectiveReprocessModal.svelte', () => ({ default: noopComponent() }));
vi.mock('$components/ConfirmationModal.svelte', () => ({ default: noopComponent() }));
vi.mock('$components/SummaryModal.svelte', () => ({ default: noopComponent() }));
vi.mock('$components/TranscriptModal.svelte', () => ({ default: noopComponent() }));
vi.mock('$components/fileDetail/TxtExportOptionsModal.svelte', () => ({
  default: noopComponent(),
}));
// FileActionButtons is deliberately NOT stubbed: it's the component under test
// for the edit-permission gating suite below. It's purely presentational
// (only depends on `$t`, already mocked, and `Spinner`, mocked just below),
// so it's safe to render for real.
vi.mock('$components/fileDetail/RedactionControls.svelte', () => ({ default: noopComponent() }));
vi.mock('$components/fileDetail/RedactionPendingPanel.svelte', () => ({
  default: noopComponent(),
}));
vi.mock('$components/fileDetail/SpeakerProfileConfirmModal.svelte', () => ({
  default: noopComponent(),
}));
vi.mock('../../../components/ui/Spinner.svelte', () => ({ default: noopComponent() }));
// Not stubbed: it's a pure presentational component (only depends on the
// already-mocked `$t`), and it doubles as the "still loading" DOM signal.

import Page from './+page.svelte';
import FileActionButtons from '$components/fileDetail/FileActionButtons.svelte';
import type { MediaFileDetail } from '$lib/types/media';

function completeFileResponse(overrides: Record<string, unknown> = {}) {
  return {
    data: {
      uuid: 'file-1',
      filename: 'meeting-notes.mp4',
      status: 'completed',
      transcript_segments: [{ uuid: 'seg-1', start_time: 0, end_time: 1, text: 'hello' }],
      grouped_segments: [],
      speakers: [],
      total_segments: 1,
      total_speaker_segments: 1,
      segment_limit: 500,
      segment_offset: 0,
      my_permission: null,
      collections: [],
      ...overrides,
    },
  };
}

beforeEach(() => {
  vi.clearAllMocks();
  mockGetAISuggestions.mockResolvedValue(null);
  mockGetMediaStreamUrl.mockResolvedValue('https://example.com/stream.mp4');
});

describe('files/[id]/+page — fetch/loading/error state machine', () => {
  it('renders the completed file once the fetch resolves, and titles the tab from it', async () => {
    mockAxios.get.mockImplementation((url: string) => {
      if (url === '/files/file-1') return Promise.resolve(completeFileResponse());
      if (url === '/speakers') return Promise.resolve({ data: [] });
      return Promise.resolve({ data: {} });
    });

    const { container } = render(Page, { props: { data: { id: 'file-1' } } });

    await waitFor(() => {
      expect(document.title).toBe('meeting-notes.mp4');
    });

    // The generic error container and the loading skeleton must both be gone
    // once the success branch is reached.
    expect(container.querySelector('.error-container')).toBeNull();
    expect(container.querySelector('.skeleton-page')).toBeNull();
  });

  it('shows the generic error state (not a blank page) when the file fetch 404s', async () => {
    const notFound = { response: { status: 404, data: { detail: 'Not found' } } };
    mockAxios.get.mockImplementation((url: string) => {
      if (url === '/files/file-missing') return Promise.reject(notFound);
      return Promise.resolve({ data: {} });
    });

    const { container } = render(Page, { props: { data: { id: 'file-missing' } } });

    await waitFor(() => {
      const errorEl = container.querySelector('.error-message');
      expect(errorEl).not.toBeNull();
      expect(errorEl?.textContent).toContain('fileDetail.failedToLoadFile');
    });

    // Retry button is present (the page's own recovery affordance), and the
    // page never reached the `{:else if file}` branch.
    expect(container.querySelector('.error-container button')).not.toBeNull();
    expect(document.title).not.toBe('meeting-notes.mp4');
  });

  it('shows the SAME generic error state — never another user’s file content — on a 403', async () => {
    const forbidden = { response: { status: 403, data: { detail: 'Not enough permissions' } } };
    mockAxios.get.mockImplementation((url: string) => {
      if (url === '/files/someone-elses-file') return Promise.reject(forbidden);
      return Promise.resolve({ data: {} });
    });

    const { container } = render(Page, { props: { data: { id: 'someone-elses-file' } } });

    await waitFor(() => {
      const errorEl = container.querySelector('.error-message');
      expect(errorEl).not.toBeNull();
      expect(errorEl?.textContent).toContain('fileDetail.failedToLoadFile');
    });

    // No filename, transcript, or any file-derived content ever reached the DOM.
    expect(container.textContent).not.toContain('someone-elses-file');
    expect(document.title).not.toBe('meeting-notes.mp4');
  });
});

describe('files/[id]/+page — edit-permission gating', () => {
  it('owner (my_permission: null) sees the reprocess and generate-summary buttons', async () => {
    mockAxios.get.mockImplementation((url: string) => {
      if (url === '/files/file-1') {
        return Promise.resolve(completeFileResponse({ my_permission: null }));
      }
      if (url === '/speakers') return Promise.resolve({ data: [] });
      return Promise.resolve({ data: {} });
    });

    const { container } = render(Page, { props: { data: { id: 'file-1' } } });

    await waitFor(() => {
      expect(document.title).toBe('meeting-notes.mp4');
    });

    expect(container.querySelector('.reprocess-button-header')).not.toBeNull();
    expect(container.querySelector('.generate-summary-btn')).not.toBeNull();
  });

  it('editor (my_permission: "editor") sees the reprocess and generate-summary buttons', async () => {
    mockAxios.get.mockImplementation((url: string) => {
      if (url === '/files/file-1') {
        return Promise.resolve(completeFileResponse({ my_permission: 'editor' }));
      }
      if (url === '/speakers') return Promise.resolve({ data: [] });
      return Promise.resolve({ data: {} });
    });

    const { container } = render(Page, { props: { data: { id: 'file-1' } } });

    await waitFor(() => {
      expect(document.title).toBe('meeting-notes.mp4');
    });

    expect(container.querySelector('.reprocess-button-header')).not.toBeNull();
    expect(container.querySelector('.generate-summary-btn')).not.toBeNull();
  });

  it('viewer (my_permission: "viewer") never sees the reprocess or generate-summary buttons', async () => {
    mockAxios.get.mockImplementation((url: string) => {
      if (url === '/files/file-1') {
        return Promise.resolve(completeFileResponse({ my_permission: 'viewer' }));
      }
      if (url === '/speakers') return Promise.resolve({ data: [] });
      return Promise.resolve({ data: {} });
    });

    const { container } = render(Page, { props: { data: { id: 'file-1' } } });

    // Confirm the page actually loaded (not just that nothing rendered).
    await waitFor(() => {
      expect(document.title).toBe('meeting-notes.mp4');
    });

    expect(container.querySelector('.reprocess-button-header')).toBeNull();
    expect(container.querySelector('.generate-summary-btn')).toBeNull();
  });

  it('pins the sentinel: an absent my_permission field is treated as owner', async () => {
    // FastAPI always emits the key in practice (MediaFileDetail.my_permission
    // defaults to None, not "missing"), so "absent" is indistinguishable on
    // the wire from `null` = owner, and is correctly treated as such.
    const response = completeFileResponse();
    delete (response.data as { my_permission?: string | null }).my_permission;

    mockAxios.get.mockImplementation((url: string) => {
      if (url === '/files/file-1') return Promise.resolve(response);
      if (url === '/speakers') return Promise.resolve({ data: [] });
      return Promise.resolve({ data: {} });
    });

    const { container } = render(Page, { props: { data: { id: 'file-1' } } });

    await waitFor(() => {
      expect(document.title).toBe('meeting-notes.mp4');
    });

    expect(container.querySelector('.reprocess-button-header')).not.toBeNull();
    expect(container.querySelector('.generate-summary-btn')).not.toBeNull();
  });

  it('FileActionButtons with no canEdit prop passed shows neither gated button (pins the default-deny)', () => {
    // Component-level test: the page always computes and passes canEdit
    // explicitly, so this default is unreachable through the page today —
    // it exists as defense-in-depth. This is the one case in this suite that
    // actually differs between old and new code (old default was `true`).
    const { container } = render(FileActionButtons, {
      props: {
        file: {
          uuid: 'file-1',
          filename: 'meeting-notes.mp4',
          status: 'completed',
          transcript_segments: [],
        } as unknown as MediaFileDetail,
      },
    });

    expect(container.querySelector('.reprocess-button-header')).toBeNull();
    expect(container.querySelector('.generate-summary-btn')).toBeNull();
  });
});
