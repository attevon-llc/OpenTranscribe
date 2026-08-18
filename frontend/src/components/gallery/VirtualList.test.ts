/**
 * `VirtualList.svelte` windows a potentially huge gallery file list down to the
 * rows near the viewport. Wrong windowing math means either dead scroll space
 * (rows missing) or a jank-inducing full re-render (windowing not narrowing at
 * all) — neither throws, so this needs a direct test rather than relying on it
 * "looking right" in the browser.
 */
import { describe, it, expect, vi, beforeEach, afterEach } from 'vitest';
import { render, fireEvent } from '@testing-library/svelte';
import { tick } from 'svelte';
import type { MediaFile } from '$lib/types/media';

const mockGalleryStore = vi.hoisted(() => ({
  handleMultiSelect: vi.fn(),
  toggleFileSelection: vi.fn(),
}));
vi.mock('$stores/gallery', () => ({ galleryStore: mockGalleryStore }));

vi.mock('$stores/locale', () => ({
  t: {
    subscribe: (run: (value: (key: string) => string) => void) => {
      run((key: string) => key);
      return () => {};
    },
  },
}));

const mockPrefetch = vi.hoisted(() => ({
  prefetchFileDetails: vi.fn(),
  cancelPrefetch: vi.fn(),
}));
vi.mock('$lib/prefetch', () => mockPrefetch);

import VirtualList from './VirtualList.svelte';
import { gotoCalls } from '../../test-mocks/app-navigation';

const ROW_HEIGHT = 44;

function file(uuid: string, overrides: Partial<MediaFile> = {}): MediaFile {
  return {
    uuid,
    filename: `${uuid}.mp3`,
    status: 'completed',
    upload_time: '2026-01-01T00:00:00Z',
    ...overrides,
  } as MediaFile;
}

function manyFiles(n: number): MediaFile[] {
  return Array.from({ length: n }, (_, i) => file(`f${i}`));
}

function scrollContainerWithHeight(height: number): HTMLElement {
  const el = document.createElement('div');
  Object.defineProperty(el, 'clientHeight', { configurable: true, value: height });
  document.body.appendChild(el);
  return el;
}

beforeEach(() => {
  vi.clearAllMocks();
  gotoCalls.length = 0;
  document.body.innerHTML = '';
  // Deterministic, synchronous scheduling — jsdom's real rAF (when present)
  // fires on a real-time frame timer, which `tick()` alone does not flush.
  vi.stubGlobal('requestAnimationFrame', (cb: FrameRequestCallback) => {
    cb(0);
    return 1;
  });
  vi.stubGlobal('cancelAnimationFrame', () => {});
});

afterEach(() => {
  vi.unstubAllGlobals();
});

describe('windowing', () => {
  it('renders only the rows near the viewport for a list much larger than it', async () => {
    const scrollContainer = scrollContainerWithHeight(400);
    const items = manyFiles(500);

    const { container } = render(VirtualList, { props: { items, scrollContainer } });
    await tick();

    const rendered = container.querySelectorAll('.file-list-row');
    // viewport rows ceil(400/44)=10, + OVERSCAN(5) on each side = up to 20
    expect(rendered.length).toBeGreaterThan(0);
    expect(rendered.length).toBeLessThan(items.length);
    expect(rendered.length).toBeLessThanOrEqual(21);
  });

  it('renders every row when the full list fits inside the viewport', async () => {
    const scrollContainer = scrollContainerWithHeight(2000);
    const items = manyFiles(5);

    const { container } = render(VirtualList, { props: { items, scrollContainer } });
    await tick();

    expect(container.querySelectorAll('.file-list-row')).toHaveLength(5);
  });

  it('shifts the rendered window forward when the container scrolls', async () => {
    const scrollContainer = scrollContainerWithHeight(400);
    const items = manyFiles(500);

    const { container } = render(VirtualList, { props: { items, scrollContainer } });
    await tick();
    const firstRowIndexBefore = container
      .querySelector('.file-list-row')
      ?.getAttribute('aria-rowindex');

    Object.defineProperty(scrollContainer, 'scrollTop', { configurable: true, value: 4400 }); // row 100
    await fireEvent.scroll(scrollContainer);
    await tick();

    const firstRowIndexAfter = container
      .querySelector('.file-list-row')
      ?.getAttribute('aria-rowindex');
    expect(Number(firstRowIndexAfter)).toBeGreaterThan(Number(firstRowIndexBefore));
  });
});

describe('items changes', () => {
  it('re-expands the visible window when items grow past the previous (smaller) item count', async () => {
    // visibleStart/visibleEnd are plain state, not derived from `items` — they're only
    // updated by recalculate(). This exercises the items-changed reactive block (see the
    // comment on it in VirtualList.svelte): it calls recalculate() directly, with no
    // tick()+measureOffset() re-run, because recalculate() here never reads post-patch DOM
    // geometry — unlike VirtualGrid's equivalent block, which does and so must defer.
    // If VirtualList's block silently stopped firing recalculate() on an items change, this
    // would still render only 3 rows (the window computed for the old, smaller item count)
    // instead of catching up to the viewport's real capacity.
    const scrollContainer = scrollContainerWithHeight(400);
    const { container, rerender } = render(VirtualList, {
      props: { items: manyFiles(3), scrollContainer },
    });
    await tick();

    // Sanity check: with only 3 items, the window can't exceed the item count.
    expect(container.querySelectorAll('.file-list-row')).toHaveLength(3);

    // Items grow well beyond the small window computed while there were only 3 of them.
    await rerender({ items: manyFiles(500), scrollContainer });

    // viewport rows ceil(400/44)=10, + OVERSCAN(5) = 15, clamped by the new item count (500)
    expect(container.querySelectorAll('.file-list-row')).toHaveLength(15);
  });
});

describe('interaction', () => {
  it('navigates to the file detail page on a plain click', async () => {
    const scrollContainer = scrollContainerWithHeight(400);
    const items = [file('f0')];

    const { container } = render(VirtualList, { props: { items, scrollContainer } });
    await tick();

    const link = container.querySelector('.file-list-link') as HTMLElement;
    await fireEvent.click(link);

    expect(gotoCalls).toContain('/files/f0');
  });

  it('does not navigate on ctrl+click — it multi-selects instead', async () => {
    const scrollContainer = scrollContainerWithHeight(400);
    const items = [file('f0')];

    const { container } = render(VirtualList, { props: { items, scrollContainer } });
    await tick();

    const link = container.querySelector('.file-list-link') as HTMLElement;
    await fireEvent.click(link, { ctrlKey: true });

    expect(gotoCalls).toHaveLength(0);
    expect(mockGalleryStore.handleMultiSelect).toHaveBeenCalledWith('f0', true, false);
  });

  it('toggles selection via the checkbox without navigating, while in selecting mode', async () => {
    const scrollContainer = scrollContainerWithHeight(400);
    const items = [file('f0')];

    const { container } = render(VirtualList, {
      props: { items, scrollContainer, isSelecting: true },
    });
    await tick();

    const checkbox = container.querySelector('.file-checkbox') as HTMLInputElement;
    await fireEvent.change(checkbox, { target: { checked: true } });

    expect(mockGalleryStore.toggleFileSelection).toHaveBeenCalledWith('f0');
    expect(gotoCalls).toHaveLength(0);
  });

  it('dispatches errorclick when the error trigger is clicked, but only when a message exists', async () => {
    const scrollContainer = scrollContainerWithHeight(400);
    const items = [file('f0', { status: 'error', last_error_message: 'disk full' })];
    const handler = vi.fn();

    const { container } = render(VirtualList, {
      props: { items, scrollContainer },
      events: { errorclick: handler },
    } as never);
    await tick();

    const trigger = container.querySelector('.error-details-trigger') as HTMLElement;
    await fireEvent.click(trigger);

    expect(handler).toHaveBeenCalledTimes(1);
    expect(gotoCalls).toHaveLength(0); // the error trigger must not also navigate
  });
});
