import { describe, it, expect, vi, beforeEach } from 'vitest';
import { render, screen, waitFor, fireEvent } from '@testing-library/svelte';

vi.mock('$lib/axios', () => {
  const axiosInstance = { get: vi.fn(), post: vi.fn(), put: vi.fn(), delete: vi.fn() };
  return {
    default: axiosInstance,
    isRequestCancelled: () => false,
  };
});

vi.mock('$stores/toast', () => ({
  toastStore: { success: vi.fn(), error: vi.fn(), info: vi.fn() },
}));

vi.mock('$stores/locale', () => ({
  // Identity translator with interpolation so assertions can match on the i18n key and
  // its params, same pattern as SearchSettings.bootstrap.test.ts / ActiveSessionsPanel.test.ts.
  t: {
    subscribe: (
      run: (value: (key: string, params?: Record<string, unknown>) => string) => void
    ) => (run((k, params) => (params ? `${k}:${JSON.stringify(params)}` : k)), () => {}),
  },
}));

import axiosInstance from '$lib/axios';
import { toastStore } from '$stores/toast';
import SearchSettings from './SearchSettings.svelte';

const get = vi.mocked(axiosInstance.get);
const post = vi.mocked(axiosInstance.post);

const REINDEX_STATUS = {
  total_files: 10,
  indexed_files: 10,
  pending_files: 0,
  in_progress: false,
  current_model: 'all-MiniLM-L6-v2',
  current_dimension: 384,
  last_indexed_at: null,
};

function mockRoutes(degraded: Record<string, unknown> | null) {
  get.mockImplementation((url: string) => {
    if (url === '/search/models') {
      return Promise.resolve({ data: { models: [], current_model_id: 'm1' } });
    }
    if (url === '/search/reindex/status') {
      return Promise.resolve({ data: REINDEX_STATUS });
    }
    if (url === '/search/index-health') {
      return Promise.resolve({ data: {} });
    }
    if (url === '/search/models/neural/status') {
      return Promise.resolve({
        data: {
          bootstrap: {
            state: 'ok',
            attempts: 0,
            last_error: null,
            retry_at: null,
            text_only_chunk_files: 0,
          },
        },
      });
    }
    if (url === '/search/degraded-embeddings') {
      return Promise.resolve({ data: degraded });
    }
    return Promise.reject(new Error(`unexpected GET ${url}`));
  });
}

describe('SearchSettings — degraded-file re-embed preview+confirm (#626)', () => {
  beforeEach(() => {
    vi.clearAllMocks();
  });

  it('shows no re-embed banner when there are no degraded files', async () => {
    mockRoutes({ total_files: 0, truncated: false, affected_users: 0, files: [] });

    render(SearchSettings);

    await waitFor(() => expect(get).toHaveBeenCalledWith('/search/degraded-embeddings'));
    expect(screen.queryByText('settings.search.reembedButton')).not.toBeInTheDocument();
  });

  it('shows the banner with the file count when degraded files exist', async () => {
    mockRoutes({ total_files: 7, truncated: false, affected_users: 2, files: [] });

    render(SearchSettings);

    await waitFor(() => {
      expect(
        screen.getByText(/settings\.search\.reembedButtonCount.*"count":7/)
      ).toBeInTheDocument();
    });
  });

  it('requires confirmation before dispatching — POST is not called on the first click', async () => {
    mockRoutes({ total_files: 7, truncated: false, affected_users: 2, files: [] });

    render(SearchSettings);

    await waitFor(() =>
      expect(screen.getByRole('button', { name: 'settings.search.reembedButton' })).toBeEnabled()
    );
    await fireEvent.click(screen.getByRole('button', { name: 'settings.search.reembedButton' }));

    // The confirmation modal is up; nothing has been dispatched yet.
    expect(post).not.toHaveBeenCalled();
    expect(
      screen.getByText(/settings\.search\.reembedModalMessage.*"count":7.*"users":2/)
    ).toBeInTheDocument();
  });

  it('dispatches only after the modal is confirmed, then reloads status', async () => {
    mockRoutes({ total_files: 7, truncated: false, affected_users: 2, files: [] });
    post.mockResolvedValue({
      data: { status: 'started', task_id: 'abc-123', message: 'ok' },
    } as never);

    render(SearchSettings);

    await waitFor(() =>
      expect(screen.getByRole('button', { name: 'settings.search.reembedButton' })).toBeEnabled()
    );
    await fireEvent.click(screen.getByRole('button', { name: 'settings.search.reembedButton' }));

    const confirmButtons = screen.getAllByRole('button', { name: 'settings.search.reembedButton' });
    await fireEvent.click(confirmButtons[confirmButtons.length - 1]);

    await waitFor(() => expect(post).toHaveBeenCalledWith('/search/reembed-degraded'));
    await waitFor(() =>
      expect(toastStore.success).toHaveBeenCalledWith('settings.search.reembedStarted')
    );
  });

  it('surfaces the truncated warning in the confirmation message when truncated', async () => {
    mockRoutes({ total_files: 5000, truncated: true, affected_users: 40, files: [] });

    render(SearchSettings);

    await waitFor(() =>
      expect(screen.getByRole('button', { name: 'settings.search.reembedButton' })).toBeEnabled()
    );
    await fireEvent.click(screen.getByRole('button', { name: 'settings.search.reembedButton' }));

    expect(
      screen.getByText(/settings\.search\.reembedModalTruncated.*"count":5000/)
    ).toBeInTheDocument();
  });

  it('reports already_running without treating it as a dispatch failure', async () => {
    mockRoutes({ total_files: 3, truncated: false, affected_users: 1, files: [] });
    post.mockResolvedValue({
      data: { status: 'already_running', task_id: null, message: 'busy' },
    } as never);

    render(SearchSettings);

    await waitFor(() =>
      expect(screen.getByRole('button', { name: 'settings.search.reembedButton' })).toBeEnabled()
    );
    await fireEvent.click(screen.getByRole('button', { name: 'settings.search.reembedButton' }));
    const confirmButtons = screen.getAllByRole('button', { name: 'settings.search.reembedButton' });
    await fireEvent.click(confirmButtons[confirmButtons.length - 1]);

    await waitFor(() =>
      expect(toastStore.info).toHaveBeenCalledWith('settings.search.reembedAlreadyRunning')
    );
    expect(toastStore.error).not.toHaveBeenCalled();
  });

  it('degrades gracefully (no banner, no crash) when the preview call fails', async () => {
    get.mockImplementation((url: string) => {
      if (url === '/search/degraded-embeddings') {
        return Promise.reject(new Error('network error'));
      }
      if (url === '/search/models') {
        return Promise.resolve({ data: { models: [], current_model_id: 'm1' } });
      }
      if (url === '/search/reindex/status') {
        return Promise.resolve({ data: REINDEX_STATUS });
      }
      if (url === '/search/index-health') {
        return Promise.resolve({ data: {} });
      }
      if (url === '/search/models/neural/status') {
        return Promise.resolve({
          data: {
            bootstrap: {
              state: 'ok',
              attempts: 0,
              last_error: null,
              retry_at: null,
              text_only_chunk_files: 0,
            },
          },
        });
      }
      return Promise.reject(new Error(`unexpected GET ${url}`));
    });

    render(SearchSettings);

    await waitFor(() => expect(get).toHaveBeenCalledWith('/search/degraded-embeddings'));
    expect(screen.queryByText('settings.search.reembedButton')).not.toBeInTheDocument();
  });
});
