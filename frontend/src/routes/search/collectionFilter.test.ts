/**
 * The search page's collection filter. The filter sidebar's collection ids are UUIDs, and the
 * page sent them as `collection_id`, an integer parameter: every collection filter answered
 * 422 and the page showed a search error. It must send `collection_uuid`, and the collection
 * must round-trip through the URL (`collection=`) so the info card's collection chips, which
 * link to `/search?q=..&collection=<uuid>`, open a filtered search.
 */
import { describe, it, expect, vi, beforeEach } from 'vitest';
import { render } from '@testing-library/svelte';
import { tick } from 'svelte';

const COLLECTION = '01a1719e-0000-7000-8000-00000000c011';

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

const mockGoto = vi.hoisted(() => vi.fn());
vi.mock('$app/navigation', () => ({ goto: mockGoto, beforeNavigate: vi.fn() }));

vi.mock('$app/stores', () => {
  const url = new URL(
    'http://localhost/search?q=roadmap&collection=01a1719e-0000-7000-8000-00000000c011'
  );
  return {
    page: {
      subscribe: (run: (value: { url: URL }) => void) => {
        run({ url });
        return () => {};
      },
    },
  };
});

vi.mock('$lib/api/mediaUrl', () => ({
  getMediaStreamUrl: vi.fn(),
  getCachedUrlInfo: () => null,
  createUrlRefresher: () => ({ stop: vi.fn() }),
  clearMediaUrlCache: vi.fn(),
}));

function noopComponent() {
  return () => {};
}
vi.mock('$components/search/SearchResultsBody.svelte', () => ({ default: noopComponent() }));
vi.mock('$components/transcript/TranscriptViewModal.svelte', () => ({ default: noopComponent() }));
vi.mock('$components/search/SearchPagination.svelte', () => ({ default: noopComponent() }));
vi.mock('$components/SummaryModal.svelte', () => ({ default: noopComponent() }));
vi.mock('$components/FilterSidebar.svelte', () => ({ default: noopComponent() }));
vi.mock('$components/search/SearchAutocomplete.svelte', () => ({ default: noopComponent() }));
vi.mock('$components/ui/SortDropdown.svelte', () => ({ default: noopComponent() }));
vi.mock('$components/FloatingPreviewPlayer.svelte', () => ({ default: noopComponent() }));
vi.mock('../../components/ui/CardGridSkeleton.svelte', () => ({ default: noopComponent() }));

import Page from './+page.svelte';

beforeEach(() => {
  vi.clearAllMocks();
  mockAxios.get.mockImplementation((url: string) => {
    if (url === '/search') {
      return Promise.resolve({
        data: {
          query: 'roadmap',
          results: [],
          total_results: 0,
          total_files: 0,
          page: 1,
          page_size: 20,
          total_pages: 0,
          search_time_ms: 1.0,
          filters_applied: {},
          search_mode: 'hybrid',
        },
      });
    }
    return Promise.resolve({ data: { neural_enabled: false, active_model_id: null } });
  });
});

async function settle() {
  for (let i = 0; i < 25; i++) {
    await Promise.resolve();
    await tick();
  }
}

describe('search/+page — collection filter', () => {
  it('sends the URL collection as collection_uuid, never as the integer collection_id', async () => {
    render(Page);
    await settle();

    const searchCalls = mockAxios.get.mock.calls.filter(([url]) => url === '/search');
    expect(searchCalls).toHaveLength(1);
    const params = searchCalls[0][1].params as Record<string, unknown>;
    expect(params.collection_uuid).toBe(COLLECTION);
    expect(params).not.toHaveProperty('collection_id');
  });

  it('keeps the collection in the page URL it writes back', async () => {
    render(Page);
    await settle();

    expect(mockGoto).toHaveBeenCalled();
    const written = new URL(String(mockGoto.mock.calls[0][0]), 'http://localhost');
    expect(written.searchParams.get('collection')).toBe(COLLECTION);
    expect(written.searchParams.get('q')).toBe('roadmap');
  });
});
