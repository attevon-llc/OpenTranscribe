/**
 * `SearchPagination.svelte` has never had a test file. It computes the windowed
 * list of page numbers/ellipses (1-5, then `current ± 2`, clamped) purely from
 * `(page, totalPages)` props, and the internal `getVisiblePages()` is not exported —
 * so these tests drive the rendered DOM (`.page-btn` / `.ellipsis`, in nav order)
 * rather than the function directly, matching the fallback pattern used for other
 * windowing components (e.g. `gallery/VirtualGrid.test.ts`) that also can't be
 * unit-tested as a pure function without touching the component beyond the fix.
 *
 * BUG THIS PINS: the ellipsis-before-the-window guard checked `current >
 * INITIAL_PAGES + 1` directly. For `total=20`, `current=7` or `current=8`, the
 * clamped `windowStart` (`max(INITIAL_PAGES + 1, current - AROUND_CURRENT)`)
 * collapses back to `INITIAL_PAGES + 1` (6) — i.e. the window is actually
 * adjacent to the initial 1-5 run, no real gap — yet the old guard still fired
 * because it only looked at `current`, rendering the nonsensical "5 … 6".
 */
import { describe, it, expect, vi } from 'vitest';
import { render } from '@testing-library/svelte';

vi.mock('$stores/locale', async () => {
  const { readable } = await import('svelte/store');
  const en = (await import('$lib/i18n/locales/en.json')).default as Record<string, string>;
  return { t: readable((key: string) => en[key] ?? key) };
});

import SearchPagination from './SearchPagination.svelte';

/** Reconstructs the rendered page-list (numbers + '...') from DOM order. */
function renderedPages(container: HTMLElement): (number | '...')[] {
  const children = Array.from(container.querySelectorAll('.pagination > *'));
  return children
    .filter((el) => !el.classList.contains('prev') && !el.classList.contains('next'))
    .map((el) =>
      el.classList.contains('ellipsis') ? '...' : Number((el.textContent ?? '').trim())
    );
}

function getVisiblePages(current: number, total: number): (number | '...')[] {
  const { container } = render(SearchPagination, { props: { page: current, totalPages: total } });
  return renderedPages(container);
}

describe('SearchPagination windowing', () => {
  it('total=20, current=7: no spurious ellipsis between 5 and 6', () => {
    expect(getVisiblePages(7, 20)).toEqual([1, 2, 3, 4, 5, 6, 7, 8, 9, '...', 20]);
  });

  it('total=20, current=8: no spurious ellipsis between 5 and 6', () => {
    expect(getVisiblePages(8, 20)).toEqual([1, 2, 3, 4, 5, 6, 7, 8, 9, 10, '...', 20]);
  });

  it('total=20, current=6: window starts immediately after the initial run', () => {
    expect(getVisiblePages(6, 20)).toEqual([1, 2, 3, 4, 5, 6, 7, 8, '...', 20]);
  });

  it('total=20, current=9: a real gap exists, ellipsis correctly shown', () => {
    expect(getVisiblePages(9, 20)).toEqual([1, 2, 3, 4, 5, '...', 7, 8, 9, 10, 11, '...', 20]);
  });

  describe('no ellipsis ever sits next to a page exactly one greater than its predecessor', () => {
    for (const total of [20, 50]) {
      it(`total=${total}`, () => {
        let ellipsisCount = 0;

        for (let current = 1; current <= total; current++) {
          const pages = getVisiblePages(current, total);

          for (let i = 0; i < pages.length; i++) {
            if (pages[i] !== '...') continue;
            ellipsisCount++;

            const prevReal = pages[i - 1];
            const nextReal = pages[i + 1];

            // Every '...' must be flanked by real page numbers (never the
            // first/last entry) — asserted directly rather than guarded, so a
            // violation fails loudly instead of being skipped.
            expect(
              typeof prevReal,
              `total=${total} current=${current}: '...' at index ${i} has no preceding page`
            ).toBe('number');
            expect(
              typeof nextReal,
              `total=${total} current=${current}: '...' at index ${i} has no following page`
            ).toBe('number');
            expect(
              (nextReal as number) - (prevReal as number),
              `total=${total} current=${current}: spurious '...' between ${prevReal} and ${nextReal}`
            ).toBeGreaterThan(1);
          }
        }

        // Guards against the loop above silently never finding an ellipsis
        // (e.g. if getVisiblePages stopped emitting '...' entirely).
        expect(ellipsisCount).toBeGreaterThan(0);
      });
    }
  });
});
