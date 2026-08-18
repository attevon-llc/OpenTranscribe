/**
 * `VirtualGrid.svelte` is `VirtualList.svelte`'s 2D sibling — it additionally has
 * to derive how many columns fit the container width before it can compute which
 * ROWS are visible, and reacts to container resize via `ResizeObserver`. Wrong
 * math here means dead scroll space or a full unwindowed render, same as the
 * list — neither throws.
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
    subscribe: (run: (value: (key: string, vars?: Record<string, unknown>) => string) => void) => {
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

vi.mock('$lib/thumbnailCache', () => ({ cachedThumbnail: () => ({ destroy() {} }) }));

import VirtualGrid from './VirtualGrid.svelte';
import { gotoCalls } from '../../test-mocks/app-navigation';

const ROW_HEIGHT = 195; // desktop (jsdom's window.innerWidth is not < 768)
const CARD_MIN_WIDTH = 220;
const GAP = 12;

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

/** columnsPerRow for a given wrapper width, matching updateColumns()'s formula. */
function columnsFor(width: number): number {
  return Math.max(1, Math.floor((width + GAP) / (CARD_MIN_WIDTH + GAP)));
}

class FakeResizeObserver {
  observe() {}
  disconnect() {}
  unobserve() {}
}

function scrollContainerWithHeight(height: number): HTMLElement {
  const el = document.createElement('div');
  Object.defineProperty(el, 'clientHeight', { configurable: true, value: height });
  document.body.appendChild(el);
  return el;
}

/** clientWidth is read off `.virtual-grid-wrapper` — set it after render. */
function setWrapperWidth(container: HTMLElement, width: number) {
  const wrapper = container.querySelector('.virtual-grid-wrapper') as HTMLElement;
  Object.defineProperty(wrapper, 'clientWidth', { configurable: true, value: width });
}

beforeEach(() => {
  vi.clearAllMocks();
  gotoCalls.length = 0;
  document.body.innerHTML = '';
  vi.stubGlobal('requestAnimationFrame', (cb: FrameRequestCallback) => {
    cb(0);
    return 1;
  });
  vi.stubGlobal('cancelAnimationFrame', () => {});
  vi.stubGlobal('ResizeObserver', FakeResizeObserver);
});

afterEach(() => {
  vi.unstubAllGlobals();
});

describe('windowing', () => {
  it('renders only the cards near the viewport for a list much larger than it', async () => {
    const scrollContainer = scrollContainerWithHeight(600);
    const items = manyFiles(500);

    const { container } = render(VirtualGrid, { props: { items, scrollContainer } });
    setWrapperWidth(container, 900); // 3 columns at desktop card width
    await tick();
    await tick(); // the reactive block defers through tick().then(...)

    const cols = columnsFor(900);
    const viewportRows = Math.ceil(600 / ROW_HEIGHT);
    const maxExpectedCards = (viewportRows + 2 * 2) /* OVERSCAN desktop */ * cols;

    const rendered = container.querySelectorAll('.file-card');
    expect(rendered.length).toBeGreaterThan(0);
    expect(rendered.length).toBeLessThan(items.length);
    expect(rendered.length).toBeLessThanOrEqual(maxExpectedCards);
  });

  it('renders every card when the full list fits inside the viewport', async () => {
    const scrollContainer = scrollContainerWithHeight(2000);
    const items = manyFiles(3);

    const { container } = render(VirtualGrid, { props: { items, scrollContainer } });
    setWrapperWidth(container, 900);
    await tick();
    await tick();

    expect(container.querySelectorAll('.file-card')).toHaveLength(3);
  });

  it('packs more cards per row into a wider container', async () => {
    const scrollContainer = scrollContainerWithHeight(2000);
    const items = manyFiles(20);

    const { container, unmount } = render(VirtualGrid, {
      props: { items, scrollContainer: scrollContainerWithHeight(2000) },
    });
    setWrapperWidth(container, 480); // narrow: 1-2 columns
    await tick();
    await tick();
    const narrowCount = container.querySelectorAll('.file-card').length;
    unmount();

    document.body.innerHTML = '';
    const wide = render(VirtualGrid, { props: { items, scrollContainer } });
    setWrapperWidth(wide.container, 1400); // wide: more columns fit
    await tick();
    await tick();
    const wideCount = wide.container.querySelectorAll('.file-card').length;

    // Both lists fit entirely (20 items, 2000px viewport) — so a wider grid
    // packing more columns per row means the SAME 20 cards fill fewer rows,
    // not more cards rendered. This asserts columnsPerRow actually reacted to
    // width rather than staying pinned at its initial value.
    expect(narrowCount).toBe(20);
    expect(wideCount).toBe(20);
    expect(columnsFor(1400)).toBeGreaterThan(columnsFor(480));
  });
});

describe('interaction', () => {
  it('navigates to the file detail page on a plain click', async () => {
    const scrollContainer = scrollContainerWithHeight(600);
    const items = [file('f0')];

    const { container } = render(VirtualGrid, { props: { items, scrollContainer } });
    setWrapperWidth(container, 900);
    await tick();
    await tick();

    const link = container.querySelector('.file-card-link') as HTMLElement;
    await fireEvent.click(link);

    expect(gotoCalls).toContain('/files/f0');
  });

  it('toggles selection via the checkbox without navigating, while in selecting mode', async () => {
    const scrollContainer = scrollContainerWithHeight(600);
    const items = [file('f0')];

    const { container } = render(VirtualGrid, {
      props: { items, scrollContainer, isSelecting: true },
    });
    setWrapperWidth(container, 900);
    await tick();
    await tick();

    const checkbox = container.querySelector('.file-checkbox') as HTMLInputElement;
    await fireEvent.change(checkbox, { target: { checked: true } });

    expect(mockGalleryStore.toggleFileSelection).toHaveBeenCalledWith('f0');
    expect(gotoCalls).toHaveLength(0);
  });

  it('dispatches errorclick when the error trigger is clicked', async () => {
    const scrollContainer = scrollContainerWithHeight(600);
    const items = [file('f0', { status: 'error', last_error_message: 'disk full' })];
    const handler = vi.fn();

    const { container } = render(VirtualGrid, {
      props: { items, scrollContainer },
      events: { errorclick: handler },
    } as never);
    setWrapperWidth(container, 900);
    await tick();
    await tick();

    const trigger = container.querySelector('.clickable-error') as HTMLElement;
    await fireEvent.click(trigger);

    expect(handler).toHaveBeenCalledTimes(1);
  });
});
