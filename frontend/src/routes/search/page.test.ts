/**
 * `search/+page.svelte` issues `GET /search` from `performSearch`, called
 * automatically from `onMount` when the URL carries a `q` param. Before issue
 * #904 unit 6, it ALSO called `prefetchNextSearchPage` (`$lib/prefetch`),
 * which set a 1-second `setTimeout` to silently issue a second `GET /search`
 * for the next page — a request nothing ever read (`$lib/apiCache`'s
 * `search:...:page:N` key had no reader), so every result page cost two
 * backend hits. That function and its call site are now deleted; this test
 * pins the fix by asserting exactly one `/search` call, using fake timers to
 * advance well past the deleted prefetch's 1000ms delay — without the timer
 * advance this test would pass vacuously even with the dead code still
 * present, since the assertion would run before the timer ever fired.
 */
import { describe, it, expect, vi, beforeEach, afterEach } from 'vitest';
import { render } from '@testing-library/svelte';
import { tick } from 'svelte';

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
const mockBeforeNavigate = vi.hoisted(() => vi.fn());
vi.mock('$app/navigation', () => ({ goto: mockGoto, beforeNavigate: mockBeforeNavigate }));

const mockPageStore = vi.hoisted(() => {
  const url = new URL('http://localhost/search?q=roadmap');
  return {
    subscribe: (run: (value: { url: URL }) => void) => {
      run({ url });
      return () => {};
    },
  };
});
vi.mock('$app/stores', () => ({ page: mockPageStore }));

vi.mock('$lib/api/mediaUrl', () => ({
  getMediaStreamUrl: vi.fn(),
  getCachedUrlInfo: () => null,
  createUrlRefresher: () => ({ stop: vi.fn() }),
  clearMediaUrlCache: vi.fn(),
}));

// Every child panel this route composes is stubbed to a no-op — this suite
// scopes to the page's own request behavior (see file header comment), not
// to whether each child panel renders, matching files/[id]/page.test.ts's
// established rationale for this route family.
function noopComponent() {
  return () => {};
}
vi.mock('$components/search/SearchResultCard.svelte', () => ({ default: noopComponent() }));
vi.mock('$components/search/SearchTranscriptModal.svelte', () => ({ default: noopComponent() }));
vi.mock('$components/search/SearchPagination.svelte', () => ({ default: noopComponent() }));
vi.mock('$components/search/SummaryResultCard.svelte', () => ({ default: noopComponent() }));
vi.mock('$components/SummaryModal.svelte', () => ({ default: noopComponent() }));
vi.mock('$components/FilterSidebar.svelte', () => ({ default: noopComponent() }));
vi.mock('$components/search/SearchAutocomplete.svelte', () => ({ default: noopComponent() }));
vi.mock('$components/ui/SortDropdown.svelte', () => ({ default: noopComponent() }));
vi.mock('$components/FloatingPreviewPlayer.svelte', () => ({ default: noopComponent() }));
vi.mock('$components/RetrievalQualityNotice.svelte', () => ({ default: noopComponent() }));
// Imported by relative path in the component, not the $components alias —
// vi.mock keys on the specifier as written in the import statement.
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
          // Deliberately > one page's worth: the deleted prefetch guarded
          // itself on `totalPages > pageNum`, so a single-page result would
          // pass this test vacuously (the old code would never schedule its
          // timer at all, regardless of whether the call site was removed).
          total_results: 100,
          total_files: 100,
          page: 1,
          page_size: 20,
          total_pages: 5,
          search_time_ms: 1.0,
          filters_applied: {},
          search_mode: 'hybrid',
        },
      });
    }
    if (url === '/search/models/neural') {
      return Promise.resolve({ data: { neural_enabled: false, active_model_id: null } });
    }
    return Promise.resolve({ data: {} });
  });
});

afterEach(() => {
  vi.useRealTimers();
});

describe('search/+page — exactly one request per search (issue #904 unit 6)', () => {
  it('issues exactly one GET /search, even well past the deleted prefetch timer window', async () => {
    vi.useFakeTimers();

    render(Page);

    // Flush the microtasks the automatic onMount search kicks off.
    for (let i = 0; i < 25; i++) {
      await Promise.resolve();
      await tick();
    }

    // Advance well past the deleted prefetch's 1000ms delay. If a stray timer
    // were still scheduled, this is what would fire its second /search call.
    await vi.advanceTimersByTimeAsync(5000);

    const searchCalls = mockAxios.get.mock.calls.filter(([url]) => url === '/search');
    expect(searchCalls).toHaveLength(1);
  });
});
