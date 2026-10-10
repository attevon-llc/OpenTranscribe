import { describe, it, expect, vi, beforeEach, afterEach } from 'vitest';
import { render, screen, fireEvent } from '@testing-library/svelte';
import { tick } from 'svelte';

const mockAxios = vi.hoisted(() => ({
  get: vi.fn(),
  post: vi.fn(),
  put: vi.fn(),
  delete: vi.fn(),
}));
vi.mock('$lib/axios', () => ({ default: mockAxios, isRequestCancelled: () => false }));

vi.mock('svelte-range-slider-pips', () => ({
  default: function RangeSliderStub() {
    return { $set: () => {}, $destroy: () => {} };
  },
}));

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
vi.mock('$components/transcript/TranscriptViewModal.svelte', () => ({ default: noopComponent() }));
vi.mock('$components/search/SearchPagination.svelte', () => ({ default: noopComponent() }));
vi.mock('$components/search/SummaryResultCard.svelte', () => ({ default: noopComponent() }));
vi.mock('$components/SummaryModal.svelte', () => ({ default: noopComponent() }));
vi.mock('$components/search/SearchAutocomplete.svelte', () => ({ default: noopComponent() }));
vi.mock('$components/ui/SortDropdown.svelte', () => ({ default: noopComponent() }));
vi.mock('$components/FloatingPreviewPlayer.svelte', () => ({ default: noopComponent() }));
vi.mock('$components/RetrievalQualityNotice.svelte', () => ({ default: noopComponent() }));
// Imported by relative path in the component, not the $components alias —
// vi.mock keys on the specifier as written in the import statement.
vi.mock('../../components/ui/CardGridSkeleton.svelte', () => ({ default: noopComponent() }));

import Page from './+page.svelte';
import GalleryFilterPanel from '$components/gallery/GalleryFilterPanel.svelte';

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
          total_pages: 1,
          search_time_ms: 1.0,
          filters_applied: {},
          search_mode: 'hybrid',
        },
      });
    }
    if (url === '/search/models/neural') {
      return Promise.resolve({ data: { neural_enabled: false, active_model_id: null } });
    }
    if (url.startsWith('/tags') || url.startsWith('/speakers') || url.startsWith('/collections')) {
      return Promise.resolve({ data: [] });
    }
    return Promise.resolve({ data: {} });
  });
});

async function settle() {
  for (let i = 0; i < 25; i++) {
    await Promise.resolve();
    await tick();
  }
}

describe('search/+page — source chips (issue #1197)', () => {
  it('exposes each source as a pressed toggle with a distinct selected state and a check', async () => {
    const { container } = render(Page);
    await settle();

    const group = container.querySelector('.source-toggle') as HTMLElement;
    const chips = Array.from(group.querySelectorAll('button'));
    expect(chips).toHaveLength(4);
    for (const chip of chips) {
      expect(chip).toHaveAttribute('aria-pressed', 'true');
      expect(chip).toHaveClass('selected');
      expect(chip.querySelector('.filter-chip-check')).not.toBeNull();
    }

    await fireEvent.click(chips[1]);
    await settle();
    // Re-query: a search round-trip may re-render the row, so the old node is stale.
    const after = Array.from(container.querySelectorAll('.source-toggle button'));
    expect(after[1]).toHaveAttribute('aria-pressed', 'false');
    expect(after[1]).not.toHaveClass('selected');
    expect(after[1].querySelector('.filter-chip-check')).toBeNull();
    expect(after[0]).toHaveAttribute('aria-pressed', 'true');
  });
});

describe('search/+page — sidebar (issue #1197)', () => {
  it('has no "Search files" input in its sidebar', async () => {
    const { container } = render(Page);
    await settle();
    expect(container.querySelector('.filter-content')).not.toBeNull();
    expect(screen.queryByText('filter.searchFiles')).toBeNull();
    expect(screen.queryByPlaceholderText('filter.searchPlaceholder')).toBeNull();
  });

  it('uses the shared FilterPanelToggle with the same markup as the gallery panel', async () => {
    const { container } = render(Page);
    await settle();
    const searchBtn = container.querySelector('.filter-toggle-btn') as HTMLElement;
    const gallery = render(GalleryFilterPanel, {
      props: {
        showFilters: true,
        searchQuery: '',
        selectedTags: [],
        selectedSpeakers: [],
        selectedCollectionId: null,
        dateRange: { from: null, to: null },
        durationRange: { min: null, max: null },
        fileSizeRange: { min: null, max: null },
        selectedFileTypes: [],
        selectedStatuses: [],
        ownershipFilter: 'all',
      },
    });
    const galleryBtn = gallery.container.querySelector('.filter-toggle-btn') as HTMLElement;
    expect(searchBtn.outerHTML.replace(/svelte-\w+/g, '')).toBe(
      galleryBtn.outerHTML.replace(/svelte-\w+/g, '')
    );
    expect(searchBtn).toHaveAttribute('aria-expanded', 'true');
    await fireEvent.click(searchBtn);
    expect(searchBtn).toHaveAttribute('aria-expanded', 'false');
  });
});

describe('FilterSidebar opt-out stays a prop (issue #1197)', () => {
  it('FilterSidebar defaults to showing the search field for consumers that want it', async () => {
    const FilterSidebar = (await import('$components/FilterSidebar.svelte')).default;
    const { findByPlaceholderText } = render(FilterSidebar, { props: {} });
    const input = await findByPlaceholderText('filter.searchPlaceholder');
    expect(input).toHaveAttribute('type', 'text');
  });
});
