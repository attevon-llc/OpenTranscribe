/**
 * The loading skeleton must match the view the user is actually in.
 *
 * `GalleryGrid` renders card placeholders unconditionally, so switching to the
 * LIST view and reloading showed a grid of cards where a table was about to
 * appear — the layout visibly jumped when the data landed. The component
 * already imported `galleryViewMode` (for the real content branch at :48); the
 * skeleton branch just never consulted it.
 */
import { describe, it, expect, vi, beforeEach } from 'vitest';
import { render } from '@testing-library/svelte';
import { get } from 'svelte/store';
import { readFileSync } from 'fs';
import { resolve } from 'path';

vi.mock('$stores/locale', () => ({
  t: {
    subscribe: (run: (value: (key: string) => string) => void) => {
      run((key: string) => key);
      return () => {};
    },
  },
}));

import GalleryGrid from './GalleryGrid.svelte';
import { galleryStore, galleryViewMode } from '$stores/gallery';

const baseProps = {
  files: [],
  loading: true,
  error: null,
  selectedCollectionId: null,
  isSelecting: false,
  selectedFiles: new Set<string>(),
  pendingNewFiles: new Set<string>(),
  pendingDeletions: new Set<string>(),
};

function setViewMode(mode: 'grid' | 'list') {
  galleryStore.setViewMode(mode);
}

describe('gallery loading skeleton matches the active view', () => {
  beforeEach(() => setViewMode('grid'));

  it('calibration: the view-mode store actually drives what the test sets', () => {
    setViewMode('list');
    expect(get(galleryViewMode)).toBe('list');
    setViewMode('grid');
    expect(get(galleryViewMode)).toBe('grid');
  });

  it('renders card placeholders in grid view', () => {
    setViewMode('grid');
    const { container } = render(GalleryGrid, { props: baseProps } as never);

    expect(container.querySelector('.card-grid-skeleton')).not.toBeNull();
    expect(container.querySelector('.file-list')).toBeNull();
  });

  it('renders a TABLE placeholder in list view, not a grid of cards', () => {
    setViewMode('list');
    const { container } = render(GalleryGrid, { props: baseProps } as never);

    expect(container.querySelector('.file-list')).not.toBeNull();
    expect(container.querySelector('.card-grid-skeleton')).toBeNull();
  });

  it('the list placeholder has the same columns and headers as the real table', () => {
    // A generic avatar/title/actions row used to stand in here, describing a
    // layout the list view never has. The placeholder now renders the real
    // column headers and one cell per column, so nothing rearranges when the
    // files land.
    setViewMode('list');
    const { container } = render(GalleryGrid, { props: baseProps } as never);

    const header = container.querySelector('.file-list-header');
    expect(header).not.toBeNull();
    expect(header?.textContent).toContain('gallery.columnTitle');
    expect(header?.textContent).toContain('gallery.columnStatus');

    // Seven data columns, matching `--file-list-columns` in file-list-grid.css.
    // NB: not `.skel-row:first-of-type` — that keys on the element TYPE, and
    // the header div is the first div sibling, so it selects nothing.
    const firstRow = container.querySelectorAll('.skel-row')[0];
    expect(firstRow.querySelectorAll('.list-cell').length).toBe(7);
  });

  it('adds the checkbox column to the placeholder in selection mode', () => {
    // The real header gains a leading cell when selecting; a placeholder that
    // did not would be a column out of step with the table replacing it.
    setViewMode('list');
    const { container } = render(GalleryGrid, {
      props: { ...baseProps, isSelecting: true },
    } as never);

    expect(container.querySelector('.file-list-header.selecting-mode')).not.toBeNull();
    const firstRow = container.querySelectorAll('.skel-row')[0];
    expect(firstRow.querySelectorAll('.list-cell').length).toBe(8);
  });

  it('the skeleton shimmers in both views', () => {
    // The sweeping highlight is a ::after on .shimmer; assert the hook exists so
    // a refactor that drops the class cannot silently remove the animation.
    // jsdom does not run the animation, so the sweep ITSELF is pinned by the
    // stylesheet assertions below rather than here.
    for (const mode of ['grid', 'list'] as const) {
      setViewMode(mode);
      const { container } = render(GalleryGrid, { props: baseProps } as never);
      expect(container.querySelectorAll('.shimmer').length).toBeGreaterThan(0);
    }
  });
});

describe('both skeletons sweep left to right, not just pulse', () => {
  // The animation is CSS that jsdom neither applies nor computes, so this reads
  // the stylesheet. Without it the only assertion on the shimmer is that a class
  // name is present — which stays green if the keyframes are deleted, and the
  // user sees a static grey block.
  const sheet = readFileSync(resolve(__dirname, '../ui/skeleton-shimmer.css'), 'utf8');

  it('translates a highlight across the placeholder', () => {
    // Starts off the left edge...
    expect(sheet).toMatch(/transform:\s*translateX\(-100%\)/);
    // ...and the keyframes carry it off the right. A pure opacity pulse would
    // satisfy neither.
    expect(sheet).toMatch(/@keyframes\s+shimmerSlide[\s\S]*?translateX\(100%\)/);
    // `linear`, specifically. The travel runs edge-to-edge OFF the element at
    // both ends, so an eased curve idles with the highlight out of frame and
    // crosses the visible part at peak speed — which is why this looked static
    // on screen while a frame diff still showed the pixels changing.
    expect(sheet).toMatch(/animation:\s*shimmerSlide\s+[\d.]+s\s+linear\s+infinite/);
    // The highlight is a moving gradient, not a flat fill.
    expect(sheet).toMatch(/linear-gradient\(\s*90deg/);
  });

  it('holds still for prefers-reduced-motion', () => {
    const reduced = sheet.slice(sheet.indexOf('prefers-reduced-motion'));
    expect(reduced).toMatch(/animation:\s*none/);
  });

  it('both gallery skeletons consume that one stylesheet', () => {
    // The sweep used to be copied into all three skeleton components, so this
    // is what stops a fourth copy — or a component quietly dropping the import
    // and rendering static grey blocks — from going unnoticed.
    const grid = readFileSync(resolve(__dirname, '../ui/CardGridSkeleton.svelte'), 'utf8');
    const list = readFileSync(resolve(__dirname, './FileListSkeleton.svelte'), 'utf8');
    expect(grid).toMatch(/import '\.\/skeleton-shimmer\.css'/);
    expect(list).toMatch(/import '\.\.\/ui\/skeleton-shimmer\.css'/);
    // ...and neither redefines it locally.
    expect(grid).not.toMatch(/@keyframes\s+shimmerSlide/);
    expect(list).not.toMatch(/@keyframes\s+shimmerSlide/);
  });

  it('the highlight is light enough to actually see against the base', () => {
    // At rgba(255,255,255,0.35) over a 0.12 slate base the sweep changed the
    // composited pixel by ~8 of 255 — measurable in a frame diff, invisible to
    // a person. This pins the contrast that fixed it.
    const base = Number(sheet.match(/rgba\(100, 116, 139, ([\d.]+)\)/)![1]);
    const highlight = Number(sheet.match(/rgba\(255, 255, 255, ([\d.]+)\) 50%/)![1]);
    expect(highlight - base).toBeGreaterThan(0.5);
  });

  it('control: a pulse-only stylesheet would fail the sweep assertion', () => {
    // Proves the check above discriminates. This is the shape the list skeleton
    // would have had if it only faded in and out.
    const pulseOnly = `.shimmer { animation: pulse 1.4s infinite; }
      @keyframes pulse { 50% { opacity: 1; } }`;
    expect(pulseOnly).not.toMatch(/@keyframes\s+shimmerSlide[\s\S]*?translateX\(100%\)/);
  });
});
