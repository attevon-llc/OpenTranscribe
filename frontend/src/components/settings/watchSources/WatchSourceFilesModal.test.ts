/**
 * `WatchSourceFilesModal` is the #489 coordinator: it owns the fetch, the filters and
 * the batching. These pin the decisions that are easy to get subtly wrong and
 * expensive when they are — filtering server-side rather than in the browser, sending
 * one batch rather than N requests, and refreshing when a scan actually lands.
 */
import { describe, it, expect, vi, beforeEach } from 'vitest';
import { render, screen, fireEvent, waitFor } from '@testing-library/svelte';

const api = vi.hoisted(() => ({
  getWatchSourceFiles: vi.fn(),
  retryWatchSourceFiles: vi.fn(),
  deleteWatchSourceFile: vi.fn(),
  bulkDeleteWatchSourceFiles: vi.fn(),
}));

vi.mock('$lib/api/watchSourcesApi', async () => {
  const actual = await vi.importActual<typeof import('$lib/api/watchSourcesApi')>(
    '$lib/api/watchSourcesApi'
  );
  return { ...actual, ...api };
});

const mockToast = vi.hoisted(() => ({
  success: vi.fn(),
  error: vi.fn(),
  warning: vi.fn(),
  info: vi.fn(),
}));
vi.mock('$stores/toast', () => ({ toastStore: mockToast }));

vi.mock('$stores/locale', () => ({
  t: {
    subscribe: (run: (value: (key: string, vars?: Record<string, unknown>) => string) => void) => {
      run((key: string) => key);
      return () => {};
    },
  },
}));

import WatchSourceFilesModal from './WatchSourceFilesModal.svelte';

const SOURCE = {
  uuid: 'w1',
  name: 'NAS recordings',
  source_type: 'local',
  is_enabled: true,
  skip_files_older_than_days: null,
} as never;

function page(files: unknown[], total = files.length) {
  return { files, total, page: 1, page_size: 50 };
}

const ERROR_ROW = {
  uuid: 'f1',
  remote_path: '/watch/broken.mp4',
  filename: 'broken.mp4',
  status: 'error',
  retry_count: 1,
  error_message: 'download produced no bytes',
  skip_reason: null,
};

beforeEach(() => {
  vi.clearAllMocks();
  api.getWatchSourceFiles.mockResolvedValue(page([ERROR_ROW]));
  api.retryWatchSourceFiles.mockResolvedValue({
    results: [{ file_uuid: 'f1', success: true, status: 'pending' }],
    scan_dispatched: true,
  });
  api.deleteWatchSourceFile.mockResolvedValue(undefined);
  api.bulkDeleteWatchSourceFiles.mockResolvedValue({
    results: [{ file_uuid: 'f1', success: true }],
    scan_dispatched: false,
  });
});

function open() {
  return render(WatchSourceFilesModal, { props: { show: true, source: SOURCE } } as never);
}

describe('loading', () => {
  it('fetches the first page for the source when opened', async () => {
    open();
    await waitFor(() => expect(api.getWatchSourceFiles).toHaveBeenCalled());
    expect(api.getWatchSourceFiles).toHaveBeenCalledWith('w1', 1, 50, undefined, undefined);
    expect(await screen.findByText('broken.mp4')).toBeInTheDocument();
  });

  it('surfaces a load failure instead of silently showing an empty table', async () => {
    // An empty table and a failed request look identical on screen, and the first is a
    // fact while the second is a bug — so the toast is the only thing distinguishing
    // them. Assert the rendered result too: reporting the error while leaving a stale
    // row list on screen would be its own kind of lie.
    api.getWatchSourceFiles.mockRejectedValueOnce(new Error('boom'));
    open();

    await waitFor(() => expect(mockToast.error).toHaveBeenCalled());
    expect(await screen.findByText('settings.watchSources.files.emptyTitle')).toBeInTheDocument();
    expect(screen.queryByText('broken.mp4')).not.toBeInTheDocument();
  });
});

describe('filters are server-side', () => {
  it('re-queries with the status when the filter changes', async () => {
    open();
    await waitFor(() => expect(api.getWatchSourceFiles).toHaveBeenCalledTimes(1));

    await fireEvent.change(screen.getByLabelText('settings.watchSources.files.columnStatus'), {
      target: { value: 'error' },
    });

    await waitFor(() =>
      expect(api.getWatchSourceFiles).toHaveBeenLastCalledWith('w1', 1, 50, 'error', undefined)
    );
  });

  it('re-queries with the search text rather than filtering in the browser', async () => {
    // A source can track more files than the client should ever hold, so filtering
    // here would mean paging the whole table into memory to hide most of it.
    vi.useFakeTimers();
    try {
      open();
      await vi.waitFor(() => expect(api.getWatchSourceFiles).toHaveBeenCalledTimes(1));

      await fireEvent.input(
        screen.getByLabelText('settings.watchSources.files.searchPlaceholder'),
        { target: { value: 'board' } }
      );
      await vi.advanceTimersByTimeAsync(400);

      expect(api.getWatchSourceFiles).toHaveBeenLastCalledWith('w1', 1, 50, undefined, 'board');
    } finally {
      vi.useRealTimers();
    }
  });
});

describe('retry', () => {
  it('sends ONE batch request for a single-row retry', async () => {
    open();
    expect(await screen.findByText('broken.mp4')).toBeInTheDocument();

    await fireEvent.click(screen.getByText('settings.watchSources.files.retry'));

    await waitFor(() => expect(api.retryWatchSourceFiles).toHaveBeenCalledWith('w1', ['f1']));
    expect(api.retryWatchSourceFiles).toHaveBeenCalledTimes(1);
  });

  it('reports the queued count, not a claim that the file was imported', async () => {
    open();
    expect(await screen.findByText('broken.mp4')).toBeInTheDocument();

    await fireEvent.click(screen.getByText('settings.watchSources.files.retry'));

    await waitFor(() =>
      expect(mockToast.success).toHaveBeenCalledWith('settings.watchSources.files.retryQueued')
    );
  });

  it('surfaces a per-row refusal from a partially successful batch', async () => {
    // The backend answers 200 with per-row outcomes, so a refusal never throws.
    // Reading only the HTTP status would report "queued" over a file that was not.
    api.retryWatchSourceFiles.mockResolvedValueOnce({
      results: [{ file_uuid: 'f1', success: false, error: 'cannot be retried' }],
      scan_dispatched: false,
    });
    open();
    expect(await screen.findByText('broken.mp4')).toBeInTheDocument();

    await fireEvent.click(screen.getByText('settings.watchSources.files.retry'));

    await waitFor(() =>
      expect(mockToast.error).toHaveBeenCalledWith('cannot be retried', expect.anything())
    );
    expect(mockToast.success).not.toHaveBeenCalled();
  });

  it('raises the backend warning when a retry cannot achieve anything', async () => {
    api.retryWatchSourceFiles.mockResolvedValueOnce({
      results: [
        { file_uuid: 'f1', success: true, status: 'pending', warning: 'age limit still set' },
      ],
      scan_dispatched: true,
    });
    open();
    expect(await screen.findByText('broken.mp4')).toBeInTheDocument();

    await fireEvent.click(screen.getByText('settings.watchSources.files.retry'));

    await waitFor(() =>
      expect(mockToast.warning).toHaveBeenCalledWith('age limit still set', expect.anything())
    );
  });

  it('reloads after a retry so a pending row is not left stale on screen', async () => {
    open();
    expect(await screen.findByText('broken.mp4')).toBeInTheDocument();
    const before = api.getWatchSourceFiles.mock.calls.length;

    await fireEvent.click(screen.getByText('settings.watchSources.files.retry'));

    await waitFor(() => expect(api.getWatchSourceFiles.mock.calls.length).toBeGreaterThan(before));
  });
});

describe('live refresh', () => {
  it('reloads when a scan for THIS source completes', async () => {
    // Retry only queues: the row sits at `pending` until a scan lands. Without this
    // the operator is left looking at a status that is already out of date.
    open();
    await waitFor(() => expect(api.getWatchSourceFiles).toHaveBeenCalledTimes(1));

    window.dispatchEvent(
      new CustomEvent('watch-source-scan', { detail: { source_uuid: 'w1', status: 'success' } })
    );

    await waitFor(() => expect(api.getWatchSourceFiles).toHaveBeenCalledTimes(2));
  });

  it('ignores a scan that completed for a different source', async () => {
    open();
    await waitFor(() => expect(api.getWatchSourceFiles).toHaveBeenCalledTimes(1));

    window.dispatchEvent(
      new CustomEvent('watch-source-scan', { detail: { source_uuid: 'other', status: 'success' } })
    );

    await new Promise((resolve) => setTimeout(resolve, 20));
    expect(api.getWatchSourceFiles).toHaveBeenCalledTimes(1);
  });
});
