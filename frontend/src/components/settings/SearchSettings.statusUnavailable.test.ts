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
  t: {
    subscribe: (
      run: (value: (key: string, params?: Record<string, unknown>) => string) => void
    ) => (run((k, params) => (params ? `${k}:${JSON.stringify(params)}` : k)), () => {}),
  },
}));

import axiosInstance from '$lib/axios';
import SearchSettings from './SearchSettings.svelte';

const get = vi.mocked(axiosInstance.get);

function statusBody(overrides: Record<string, unknown>) {
  return {
    total_files: 4,
    indexed_files: 2,
    pending_files: 2,
    in_progress: false,
    stop_requested: false,
    status_unavailable: false,
    current_model: 'all-MiniLM-L6-v2',
    current_dimension: 384,
    last_indexed_at: null,
    ...overrides,
  };
}

function mockRoutes(statusResponses: Array<Record<string, unknown>>) {
  let call = 0;
  get.mockImplementation((url: string) => {
    if (url === '/search/models') {
      return Promise.resolve({ data: { models: [], current_model_id: 'm1' } });
    }
    if (url === '/search/reindex/status') {
      const body = statusResponses[Math.min(call, statusResponses.length - 1)];
      call += 1;
      return Promise.resolve({ data: statusBody(body) });
    }
    if (url === '/search/index-health') return Promise.resolve({ data: {} });
    if (url === '/search/models/neural/status') {
      return Promise.resolve({ data: { bootstrap: null } });
    }
    return Promise.reject(new Error(`unexpected GET ${url}`));
  });
}

const statusCalls = () => get.mock.calls.filter(([url]) => url === '/search/reindex/status').length;

describe('SearchSettings — reindex status unavailable (fail-open fix)', () => {
  beforeEach(() => {
    vi.clearAllMocks();
  });

  afterEach(() => {
    vi.useRealTimers();
  });

  it('shows a neutral unavailable notice and offers no reindex actions', async () => {
    mockRoutes([{ status_unavailable: true }]);

    render(SearchSettings);

    await waitFor(() => {
      expect(screen.getByText('settings.search.statusUnavailable')).toBeInTheDocument();
    });
    expect(screen.getByText('search.reindexAll').closest('button')).toBeDisabled();
    expect(screen.queryByText('settings.search.reindexPending')).not.toBeInTheDocument();
  });

  it('shows no notice and keeps actions enabled when the status is known', async () => {
    mockRoutes([{ status_unavailable: false }]);

    render(SearchSettings);

    await waitFor(() => {
      expect(screen.getByText('search.reindexAll')).toBeInTheDocument();
    });
    expect(screen.queryByText('settings.search.statusUnavailable')).not.toBeInTheDocument();
    expect(screen.getByText('search.reindexAll').closest('button')).not.toBeDisabled();
  });

  it('keeps polling and recovers once the status becomes known again', async () => {
    mockRoutes([{ status_unavailable: true }, { status_unavailable: false }]);
    vi.useFakeTimers({ shouldAdvanceTime: true });

    render(SearchSettings);

    await waitFor(() => {
      expect(screen.getByText('settings.search.statusUnavailable')).toBeInTheDocument();
    });
    const before = statusCalls();

    await vi.advanceTimersByTimeAsync(10000);

    await waitFor(() => {
      expect(statusCalls()).toBeGreaterThan(before);
      expect(screen.queryByText('settings.search.statusUnavailable')).not.toBeInTheDocument();
    });
    expect(screen.getByText('search.reindexAll').closest('button')).not.toBeDisabled();

    // Known status: the retry timer must be gone, not left polling forever.
    const afterRecovery = statusCalls();
    await vi.advanceTimersByTimeAsync(60000);
    expect(statusCalls()).toBe(afterRecovery);
  });

  it('starts no poll timer when it unmounts before the first status response resolves', async () => {
    vi.useFakeTimers({ shouldAdvanceTime: true });
    let resolveStatus: (value: unknown) => void = () => {};
    get.mockImplementation((url: string) => {
      if (url === '/search/reindex/status') {
        return new Promise((resolve) => {
          resolveStatus = resolve;
        });
      }
      if (url === '/search/models') {
        return Promise.resolve({ data: { models: [], current_model_id: 'm1' } });
      }
      if (url === '/search/index-health') return Promise.resolve({ data: {} });
      if (url === '/search/models/neural/status') {
        return Promise.resolve({ data: { bootstrap: null } });
      }
      return Promise.reject(new Error(`unexpected GET ${url}`));
    });

    const { unmount } = render(SearchSettings);
    await waitFor(() => expect(statusCalls()).toBe(1));

    unmount();
    resolveStatus({ data: statusBody({ status_unavailable: true }) });
    await vi.advanceTimersByTimeAsync(35000);

    expect(statusCalls()).toBe(1);
  });

  it('still renders the live-progress panel with a stop button while a run is in progress', async () => {
    mockRoutes([{ in_progress: true }]);

    render(SearchSettings);

    await waitFor(() => {
      expect(screen.getByText('search.stopReindex')).toBeInTheDocument();
    });
    expect(screen.queryByText('settings.search.statusUnavailable')).not.toBeInTheDocument();
  });
});
