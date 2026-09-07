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

// The key stands in for the copy, but interpolated values are appended: a
// message whose whole job is to report a number ("showing the first 200") is
// only testable if the number survives the stub.
vi.mock('$stores/locale', () => ({
  t: {
    subscribe: (run: (value: (key: string, vars?: Record<string, unknown>) => string) => void) => {
      run((key: string, vars?: Record<string, unknown>) =>
        vars ? `${key} ${Object.values(vars).join(' ')}` : key
      );
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
//
// The slider stub RECORDS the props it was constructed with, because #744's
// bug is entirely about what `min`/`max` the control is given and *when*: a
// slider handed the hardcoded 0–3600 seed instead of the library's real bounds
// produces a range that matches every file, which is what "moving the slider
// does not filter" looked like.
const sliderMounts = vi.hoisted(() => [] as unknown[][]);
vi.mock('svelte-range-slider-pips', () => ({
  default: function RangeSliderStub(this: unknown, ...args: unknown[]) {
    sliderMounts.push(args);
    return { $set: () => {}, $destroy: () => {} };
  },
}));

/** The props a recorded slider construction was given, whatever call shape Svelte used. */
function sliderProps(index: number): Record<string, unknown> {
  const args = sliderMounts[index] ?? [];
  for (const arg of args) {
    if (arg && typeof arg === 'object') {
      const candidate = arg as Record<string, unknown>;
      if (candidate.props && typeof candidate.props === 'object') {
        return candidate.props as Record<string, unknown>;
      }
      if ('min' in candidate && 'max' in candidate) return candidate;
    }
  }
  throw new Error(`no slider props recorded at ${index}: ${JSON.stringify(args)}`);
}
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

function speaker(label: string, { unnamed = false, count = 1 } = {}) {
  return {
    uuid: `spk-${label}`,
    name: unnamed ? label : 'SPEAKER_00',
    display_name: unnamed ? null : label,
    media_count: count,
    is_unnamed: unnamed,
  };
}

const METADATA = {
  duration: { min: 0, max: 100 },
  file_size: { min: 0, max: 100 },
  languages: [],
};

beforeEach(() => {
  vi.clearAllMocks();
  sliderMounts.length = 0;
  mockListTags.mockResolvedValue([tag('urgent', 3), tag('empty-tag', 0)]);
  mockAxios.get.mockImplementation((url: string) => {
    if (url === '/speakers') return Promise.resolve({ data: [] });
    if (url === '/files/metadata-filters') return Promise.resolve({ data: METADATA });
    return Promise.resolve({ data: {} });
  });
});

/**
 * Issue #743 — the roster only ever contained speakers a HUMAN had renamed, so
 * a freshly-diarized library filtered by nothing at all. The sidebar now opts
 * in to unlabeled speakers and, because a `SPEAKER_nn` placeholder is scoped to
 * one file and is not a person, collapses them into a single
 * "files with unlabeled speakers" facet instead of listing them as people.
 */
describe('FilterSidebar — unlabeled speakers (#743)', () => {
  it('asks the API for unlabeled speakers as well as named ones', async () => {
    render(FilterSidebarTestHost);

    await waitFor(() => expect(mockAxios.get).toHaveBeenCalledWith('/speakers', expect.anything()));
    const [, config] = mockAxios.get.mock.calls.find(([url]) => url === '/speakers')!;
    expect(config.params.include_unnamed).toBe(true);
    // One more than the page size — the over-full page IS the truncation signal.
    expect(config.params.limit).toBe(201);
  });

  it('never renders an unlabeled placeholder as a person', async () => {
    mockAxios.get.mockImplementation((url: string) => {
      if (url === '/speakers') {
        return Promise.resolve({
          data: [speaker('Priya Patel'), speaker('SPEAKER_00', { unnamed: true, count: 4 })],
        });
      }
      if (url === '/files/metadata-filters') return Promise.resolve({ data: METADATA });
      return Promise.resolve({ data: {} });
    });

    const { container } = render(FilterSidebarTestHost);
    // The person, and the aggregate facet — never a button per placeholder.
    await waitFor(() => expect(container.querySelectorAll('.speaker-button').length).toBe(2));

    const labels = Array.from(container.querySelectorAll('.speaker-button')).map(
      (el) => el.textContent?.trim()
    );
    expect(labels.some((l) => l?.startsWith('Priya Patel'))).toBe(true);
    expect(labels.some((l) => l?.includes('SPEAKER_00'))).toBe(false);
    expect(container.querySelector('[data-testid="unlabeled-speakers-facet"]')).not.toBeNull();
  });

  it('offers one aggregate facet that selects every unlabeled placeholder at once', async () => {
    mockAxios.get.mockImplementation((url: string) => {
      if (url === '/speakers') {
        return Promise.resolve({
          data: [
            speaker('SPEAKER_00', { unnamed: true }),
            speaker('SPEAKER_01', { unnamed: true }),
          ],
        });
      }
      if (url === '/files/metadata-filters') return Promise.resolve({ data: METADATA });
      return Promise.resolve({ data: {} });
    });

    const events: Array<{ speaker: string[] }> = [];
    const { container } = render(FilterSidebarTestHost, {
      props: { onFilter: (detail: unknown) => events.push(detail as { speaker: string[] }) },
    });
    const facet = (await waitFor(() => {
      const el = container.querySelector('[data-testid="unlabeled-speakers-facet"]');
      expect(el).not.toBeNull();
      return el;
    })) as HTMLElement;

    await fireEvent.click(facet);

    expect(events[events.length - 1].speaker).toEqual(['SPEAKER_00', 'SPEAKER_01']);
  });
});

/**
 * Issue #743(a) — the catch set `allSpeakers = []`, so a 500 rendered exactly
 * like "you have no named speakers": no error, no retry, no way to tell the
 * filter was broken rather than empty.
 */
describe('FilterSidebar — speaker fetch failures are visible (#743a)', () => {
  it('shows an error with a retry instead of an empty roster', async () => {
    mockAxios.get.mockImplementation((url: string) => {
      if (url === '/speakers') return Promise.reject(new Error('boom'));
      if (url === '/files/metadata-filters') return Promise.resolve({ data: METADATA });
      return Promise.resolve({ data: {} });
    });

    const { container } = render(FilterSidebarTestHost);

    await waitFor(() =>
      expect(container.querySelector('[data-testid="speakers-retry"]')).not.toBeNull()
    );
    expect(container.textContent).not.toContain('filter.noSpeakersDetected');
  });

  it('retry re-requests and recovers', async () => {
    let fail = true;
    mockAxios.get.mockImplementation((url: string) => {
      if (url === '/speakers') {
        return fail
          ? Promise.reject(new Error('boom'))
          : Promise.resolve({ data: [speaker('Priya Patel')] });
      }
      if (url === '/files/metadata-filters') return Promise.resolve({ data: METADATA });
      return Promise.resolve({ data: {} });
    });

    const { container } = render(FilterSidebarTestHost);
    const retry = (await waitFor(() => {
      const el = container.querySelector('[data-testid="speakers-retry"]');
      expect(el).not.toBeNull();
      return el;
    })) as HTMLElement;

    fail = false;
    await fireEvent.click(retry);

    await waitFor(() => expect(container.querySelectorAll('.speaker-button').length).toBe(1));
  });
});

/**
 * Issue #743(b) — the roster is capped server-side and the sidebar neither
 * asked for a page size nor said the list had been cut, so at scale speakers
 * were simply missing with nothing on screen to say so.
 */
describe('FilterSidebar — roster truncation is signalled (#743b)', () => {
  it('flags truncation when the server has more speakers than one page', async () => {
    // One more row than the page size is exactly how truncation is detected —
    // an over-full page is the only answer that distinguishes "capped" from
    // "this is all of them".
    const overfull = Array.from({ length: 201 }, (_, i) => speaker(`Person ${i}`));
    mockAxios.get.mockImplementation((url: string) => {
      if (url === '/speakers') return Promise.resolve({ data: overfull });
      if (url === '/files/metadata-filters') return Promise.resolve({ data: METADATA });
      return Promise.resolve({ data: {} });
    });

    const { container } = render(FilterSidebarTestHost);

    const notice = (await waitFor(() => {
      const el = container.querySelector('[data-testid="speakers-truncated"]');
      expect(el).not.toBeNull();
      return el;
    })) as HTMLElement;

    // It must name how many are being shown, not just say "some are missing".
    expect(notice.textContent).toContain('200');
  });

  it('says nothing about truncation for a roster that fits', async () => {
    mockAxios.get.mockImplementation((url: string) => {
      if (url === '/speakers') return Promise.resolve({ data: [speaker('Priya Patel')] });
      if (url === '/files/metadata-filters') return Promise.resolve({ data: METADATA });
      return Promise.resolve({ data: {} });
    });

    const { container } = render(FilterSidebarTestHost);
    await waitFor(() => expect(container.querySelectorAll('.speaker-button').length).toBe(1));

    expect(container.querySelector('[data-testid="speakers-truncated"]')).toBeNull();
  });
});

/**
 * Issue #744 — the duration control did not filter. The wiring was fine; the
 * BOUNDS were not. `durationBounds` is seeded `{min: 0, max: 3600}` and only
 * corrected from `/files/metadata-filters`, and the slider was rendered (and
 * draggable) before that answered — and kept rendering against the fabricated
 * seed when the request FAILED, because the catch just set `metadataLoaded`
 * and carried on. Dragging inside a 0–3600 range on a library of 100-second
 * files produces a range every file satisfies, so nothing appears to happen.
 *
 * The `at-bound → null` collapse is correct and is left alone; what is fixed
 * is ever showing a control whose bounds are made up.
 */
describe('FilterSidebar — duration slider bounds (#744)', () => {
  it('does not render the sliders until the real bounds have arrived', async () => {
    let resolveMetadata: (value: unknown) => void = () => {};
    mockAxios.get.mockImplementation((url: string) => {
      if (url === '/speakers') return Promise.resolve({ data: [] });
      if (url === '/files/metadata-filters') {
        return new Promise((resolve) => {
          resolveMetadata = resolve;
        });
      }
      return Promise.resolve({ data: {} });
    });

    const { container } = render(FilterSidebarTestHost);
    await waitFor(() => expect(container.querySelectorAll('.tag-button').length).toBe(2));

    expect(sliderMounts).toHaveLength(0);

    resolveMetadata({ data: METADATA });
    await waitFor(() => expect(sliderMounts.length).toBeGreaterThan(0));
  });

  it('gives the duration slider the library bounds, never the hardcoded seed', async () => {
    render(FilterSidebarTestHost);

    await waitFor(() => expect(sliderMounts.length).toBeGreaterThan(0));
    const duration = sliderProps(0);
    expect(duration.min).toBe(0);
    expect(duration.max).toBe(100);
  });

  it('surfaces a retry instead of fabricated 0–3600 bounds when metadata fails', async () => {
    mockAxios.get.mockImplementation((url: string) => {
      if (url === '/speakers') return Promise.resolve({ data: [] });
      if (url === '/files/metadata-filters') return Promise.reject(new Error('boom'));
      return Promise.resolve({ data: {} });
    });

    const { container } = render(FilterSidebarTestHost);

    await waitFor(() =>
      expect(container.querySelector('[data-testid="metadata-retry"]')).not.toBeNull()
    );
    expect(sliderMounts).toHaveLength(0);
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
