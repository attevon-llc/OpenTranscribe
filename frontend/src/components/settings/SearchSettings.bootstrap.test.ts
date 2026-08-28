import { describe, it, expect, vi, beforeEach, afterEach } from 'vitest';
import { render, screen, waitFor } from '@testing-library/svelte';

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
  // Identity translator with interpolation so assertions can match on the
  // i18n key and its params, same pattern as ActiveSessionsPanel.test.ts.
  t: {
    subscribe: (
      run: (value: (key: string, params?: Record<string, unknown>) => string) => void
    ) => (run((k, params) => (params ? `${k}:${JSON.stringify(params)}` : k)), () => {}),
  },
}));

import axiosInstance from '$lib/axios';
import SearchSettings from './SearchSettings.svelte';

const get = vi.mocked(axiosInstance.get);

function mockRoutes(bootstrap: Record<string, unknown> | null) {
  get.mockImplementation((url: string) => {
    if (url === '/search/models') {
      return Promise.resolve({ data: { models: [], current_model_id: 'm1' } });
    }
    if (url === '/search/reindex/status') {
      return Promise.resolve({
        data: {
          total_files: 0,
          indexed_files: 0,
          pending_files: 0,
          in_progress: false,
          current_model: 'all-MiniLM-L6-v2',
          current_dimension: 384,
          last_indexed_at: null,
        },
      });
    }
    if (url === '/search/index-health') {
      return Promise.resolve({ data: {} });
    }
    if (url === '/search/models/neural/status') {
      return Promise.resolve({ data: { bootstrap } });
    }
    return Promise.reject(new Error(`unexpected GET ${url}`));
  });
}

describe('SearchSettings — neural bootstrap banner (#625)', () => {
  beforeEach(() => {
    vi.clearAllMocks();
  });

  afterEach(() => {
    vi.useRealTimers();
  });

  it('shows the degraded banner with attempts, last error, retry time and file count', async () => {
    mockRoutes({
      state: 'degraded',
      attempts: 3,
      last_error: 'register_deploy: Could not deploy default model',
      retry_at: '2026-08-28T12:00:00Z',
      text_only_chunk_files: 5,
    });

    render(SearchSettings);

    await waitFor(() => {
      expect(screen.getByText('settings.search.bootstrapDegradedTitle')).toBeInTheDocument();
    });

    expect(
      screen.getByText(/settings\.search\.bootstrapDegradedBody.*"attempts":3/)
    ).toBeInTheDocument();
    expect(
      screen.getByText(/settings\.search\.bootstrapLastError.*register_deploy/)
    ).toBeInTheDocument();
    expect(screen.getByText(/settings\.search\.bootstrapRetryAt/)).toBeInTheDocument();
    expect(
      screen.getByText(/settings\.search\.bootstrapTextOnlyFiles.*"count":5/)
    ).toBeInTheDocument();
  });

  it('renders no banner at all when the bootstrap is healthy', async () => {
    mockRoutes({
      state: 'ok',
      attempts: 0,
      last_error: null,
      retry_at: null,
      text_only_chunk_files: 0,
    });

    render(SearchSettings);

    await waitFor(() => {
      expect(get).toHaveBeenCalledWith('/search/models/neural/status');
    });

    expect(screen.queryByText('settings.search.bootstrapDegradedTitle')).not.toBeInTheDocument();
  });

  it('degrades gracefully (no banner, no crash) when the status call fails', async () => {
    get.mockImplementation((url: string) => {
      if (url === '/search/models/neural/status') {
        return Promise.reject(new Error('network error'));
      }
      if (url === '/search/models') {
        return Promise.resolve({ data: { models: [], current_model_id: 'm1' } });
      }
      if (url === '/search/reindex/status') {
        return Promise.resolve({
          data: {
            total_files: 0,
            indexed_files: 0,
            pending_files: 0,
            in_progress: false,
            current_model: 'all-MiniLM-L6-v2',
            current_dimension: 384,
            last_indexed_at: null,
          },
        });
      }
      if (url === '/search/index-health') {
        return Promise.resolve({ data: {} });
      }
      return Promise.reject(new Error(`unexpected GET ${url}`));
    });

    render(SearchSettings);

    await waitFor(() => {
      expect(get).toHaveBeenCalledWith('/search/models/neural/status');
    });

    expect(screen.queryByText('settings.search.bootstrapDegradedTitle')).not.toBeInTheDocument();
  });
});
