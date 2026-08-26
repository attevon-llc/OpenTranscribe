/**
 * `FilterSidebar.svelte` owns the gallery's faceted-filter state and turns it
 * into a `filter` custom event carrying a plain object (`{ search, tags,
 * speaker, collectionId, language, dates, durationRange, fileSizeRange,
 * fileTypes, statuses, ownership }`) — the PARENT route is what serializes
 * that into URL params (`+page.svelte`), not this component. So "emits both
 * in the resulting query/URL params" is tested here as "the dispatched
 * `filter` event's `tags` array contains both selections" — this component's
 * actual contract with its consumer.
 *
 * Confirmed by reading the component: tag/speaker/file-type/status selection
 * is immediate (`triggerFiltersImmediate`), not debounced — only the free-text
 * search box and the two range sliders go through the 400ms debounce. `reset`
 * clears every piece of local state AND dispatches a `reset` event.
 *
 * Zero-result facets: `allTags.slice(0, 6)` is rendered unconditionally by
 * whatever `/tags` returns — there is no `usage_count > 0` filter anywhere in
 * this component, so a tag with `usage_count: 0` renders exactly like any
 * other (this is the actual behavior — the task description's "greyed out"
 * assumption does not hold; it is not disabled or styled differently, it is
 * simply not excluded from the list).
 */
import { describe, it, expect, vi, beforeEach } from 'vitest';
import { render, fireEvent, waitFor } from '@testing-library/svelte';

vi.mock('$stores/locale', () => ({
  t: {
    subscribe: (run: (value: (key: string, vars?: Record<string, unknown>) => string) => void) => {
      run((key: string) => key);
      return () => {};
    },
  },
}));

vi.mock('$lib/i18n', () => ({ translateSpeakerLabel: (name: string) => name }));

const mockListTags = vi.hoisted(() => vi.fn());
vi.mock('$lib/api/tags', () => ({ listTags: mockListTags }));

const mockAxios = vi.hoisted(() => ({ get: vi.fn() }));
vi.mock('../lib/axios', () => ({ default: mockAxios }));

// Bypass the real cache (a module-level singleton that would otherwise leak
// state across tests) — just call straight through to the fetcher.
vi.mock('$lib/apiCache', () => ({
  apiCache: { getOrFetch: (_key: string, fetchFn: () => unknown) => fetchFn() },
  cacheKey: { tags: () => 'tags', speakers: () => 'speakers', metadataFilters: () => 'meta' },
  CacheTTL: { TAGS: 0, SPEAKERS: 0, METADATA: 0 },
}));

// Heavy third-party widgets not under test here — stub to keep the DOM small
// and avoid unrelated jsdom incompatibilities.
vi.mock('svelte-range-slider-pips', () => ({
  default: () => ({ $set: () => {}, $destroy: () => {} }),
}));
vi.mock('@svelte-plugins/datepicker', () => ({
  DatePicker: () => ({ $set: () => {}, $destroy: () => {} }),
}));
vi.mock('./CollectionsFilter.svelte', () => ({
  default: () => ({ $set: () => {}, $destroy: () => {} }),
}));

// Svelte 5 removed `component.$on(...)`, so dispatched events are only
// observable through an `on:event` listener in a consumer's markup.
import FilterSidebarTestHost from './FilterSidebarTestHost.svelte';

function tag(name: string, usage_count = 5) {
  return { uuid: `tag-${name}`, name, usage_count };
}

beforeEach(() => {
  vi.clearAllMocks();
  mockListTags.mockResolvedValue([tag('urgent', 3), tag('empty-tag', 0)]);
  mockAxios.get.mockImplementation((url: string) => {
    if (url === '/speakers') return Promise.resolve({ data: [] });
    if (url === '/files/metadata-filters') {
      return Promise.resolve({
        data: { duration: { min: 0, max: 100 }, file_size: { min: 0, max: 100 }, languages: [] },
      });
    }
    return Promise.resolve({ data: {} });
  });
});

describe('FilterSidebar — facet selection', () => {
  it('selecting two tag facets includes both in the dispatched filter event', async () => {
    const events: unknown[] = [];
    const { container } = render(FilterSidebarTestHost, {
      props: { onFilter: (detail: unknown) => events.push(detail) },
    });
    await waitFor(() => expect(container.querySelectorAll('.tag-button').length).toBe(2));

    const buttons = Array.from(container.querySelectorAll('.tag-button')) as HTMLElement[];
    await fireEvent.click(buttons[0]); // urgent
    await fireEvent.click(buttons[1]); // empty-tag

    const last = events[events.length - 1] as { tags: string[] };
    expect(last.tags).toEqual(['urgent', 'empty-tag']);
  });

  it('a zero-usage-count tag still renders in the facet list (not excluded)', async () => {
    const { container } = render(FilterSidebarTestHost);
    await waitFor(() => expect(container.querySelectorAll('.tag-button').length).toBe(2));

    const labels = Array.from(container.querySelectorAll('.tag-button')).map(
      (el) => el.textContent?.trim()
    );
    expect(labels.some((l) => l?.startsWith('empty-tag'))).toBe(true);
  });
});

describe('FilterSidebar — reset', () => {
  it('clearing all filters deselects every facet and dispatches reset', async () => {
    let resetFired = false;
    const { container } = render(FilterSidebarTestHost, {
      props: { onReset: () => (resetFired = true) },
    });
    await waitFor(() => expect(container.querySelectorAll('.tag-button').length).toBe(2));

    const buttons = Array.from(container.querySelectorAll('.tag-button')) as HTMLElement[];
    await fireEvent.click(buttons[0]);
    expect(buttons[0].classList.contains('selected')).toBe(true);

    const resetBtn = container.querySelector('.reset-button') as HTMLElement;
    await fireEvent.click(resetBtn);

    expect(resetFired).toBe(true);
    // Source of truth is the button's reactive `selected` class, driven by
    // the component's own `selectedTags` array.
    await waitFor(() => expect(buttons[0].classList.contains('selected')).toBe(false));
  });
});
