/**
 * Consolidates the two near-identical sort dropdowns this replaced
 * (`gallery/GallerySortDropdown.svelte`, `search/SearchSortDropdown.svelte` — see H2).
 * Coverage below is the union of both: gallery-style options (no `noDirection` entries)
 * and search-style options (a `relevance` entry with `noDirection: true`).
 */
import { describe, it, expect, vi } from 'vitest';
import { render, screen, fireEvent } from '@testing-library/svelte';
import SortDropdown, { type SortOption } from './SortDropdown.svelte';

vi.mock('$stores/locale', () => ({
  t: {
    subscribe: (run: (value: (key: string) => string) => void) => {
      run((key: string) => key);
      return () => {};
    },
  },
}));

const gallerySortOptions: SortOption[] = [
  { value: 'upload_time', label: 'gallery.sort.uploadDate' },
  { value: 'filename', label: 'gallery.sort.filename' },
  { value: 'duration', label: 'gallery.sort.duration' },
];

const searchSortOptions: SortOption[] = [
  { value: 'relevance', label: 'search.sort.relevance', noDirection: true },
  { value: 'upload_time', label: 'gallery.sort.uploadDate' },
  { value: 'filename', label: 'gallery.sort.filename' },
];

describe('SortDropdown', () => {
  it('renders the current option label and the aria-label from ariaLabelKey', () => {
    render(SortDropdown, {
      props: {
        sortOptions: gallerySortOptions,
        sortBy: 'upload_time',
        sortOrder: 'desc',
        ariaLabelKey: 'gallery.sort.label',
      },
    });

    expect(screen.getByText('gallery.sort.uploadDate')).toBeInTheDocument();
    expect(screen.getByRole('button', { name: 'gallery.sort.label' })).toBeInTheDocument();
  });

  it('opens the menu on click and lists every option', async () => {
    render(SortDropdown, {
      props: {
        sortOptions: gallerySortOptions,
        sortBy: 'upload_time',
        sortOrder: 'desc',
        ariaLabelKey: 'gallery.sort.label',
      },
    });

    await fireEvent.click(screen.getByRole('button', { name: 'gallery.sort.label' }));

    expect(screen.getByText('gallery.sort.filename')).toBeInTheDocument();
    expect(screen.getByText('gallery.sort.duration')).toBeInTheDocument();
  });

  it('toggles direction when the active option is clicked again', async () => {
    const onChange = vi.fn();
    render(SortDropdown, {
      props: {
        sortOptions: gallerySortOptions,
        sortBy: 'upload_time',
        sortOrder: 'desc',
        ariaLabelKey: 'gallery.sort.label',
      },
      events: { change: onChange },
    });

    await fireEvent.click(screen.getByRole('button', { name: 'gallery.sort.label' }));
    await fireEvent.click(
      screen.getByText('gallery.sort.uploadDate', { selector: '.option-label' })
    );

    expect(onChange).toHaveBeenCalledTimes(1);
    expect((onChange.mock.calls[0][0] as CustomEvent).detail).toEqual({
      sortBy: 'upload_time',
      sortOrder: 'asc',
    });
  });

  it('defaults filename to ascending when switching to it from another field', async () => {
    const onChange = vi.fn();
    render(SortDropdown, {
      props: {
        sortOptions: gallerySortOptions,
        sortBy: 'upload_time',
        sortOrder: 'desc',
        ariaLabelKey: 'gallery.sort.label',
      },
      events: { change: onChange },
    });

    await fireEvent.click(screen.getByRole('button', { name: 'gallery.sort.label' }));
    await fireEvent.click(screen.getByText('gallery.sort.filename'));

    expect((onChange.mock.calls[0][0] as CustomEvent).detail).toEqual({
      sortBy: 'filename',
      sortOrder: 'asc',
    });
  });

  it('defaults a non-filename field to descending when switching to it', async () => {
    const onChange = vi.fn();
    render(SortDropdown, {
      props: {
        sortOptions: gallerySortOptions,
        sortBy: 'filename',
        sortOrder: 'asc',
        ariaLabelKey: 'gallery.sort.label',
      },
      events: { change: onChange },
    });

    await fireEvent.click(screen.getByRole('button', { name: 'gallery.sort.label' }));
    await fireEvent.click(screen.getByText('gallery.sort.duration'));

    expect((onChange.mock.calls[0][0] as CustomEvent).detail).toEqual({
      sortBy: 'duration',
      sortOrder: 'desc',
    });
  });

  it('a noDirection option (relevance) always dispatches desc and never toggles', async () => {
    const onChange = vi.fn();
    render(SortDropdown, {
      props: {
        sortOptions: searchSortOptions,
        sortBy: 'relevance',
        sortOrder: 'desc',
        ariaLabelKey: 'search.sort.label',
      },
      events: { change: onChange },
    });

    await fireEvent.click(screen.getByRole('button', { name: 'search.sort.label' }));
    // Click the already-active relevance option again.
    await fireEvent.click(screen.getByText('search.sort.relevance', { selector: '.option-label' }));

    expect((onChange.mock.calls[0][0] as CustomEvent).detail).toEqual({
      sortBy: 'relevance',
      sortOrder: 'desc',
    });
  });

  it('switching from relevance to a directional field starts at desc', async () => {
    const onChange = vi.fn();
    render(SortDropdown, {
      props: {
        sortOptions: searchSortOptions,
        sortBy: 'relevance',
        sortOrder: 'desc',
        ariaLabelKey: 'search.sort.label',
      },
      events: { change: onChange },
    });

    await fireEvent.click(screen.getByRole('button', { name: 'search.sort.label' }));
    await fireEvent.click(screen.getByText('gallery.sort.uploadDate'));

    expect((onChange.mock.calls[0][0] as CustomEvent).detail).toEqual({
      sortBy: 'upload_time',
      sortOrder: 'desc',
    });
  });

  // The arrow's geometry is drawn, not rotated by CSS, so jsdom can see which way it points:
  // an arrow whose head sits ABOVE the shaft's midpoint points up, below points down.
  function arrowPoints(svg: Element | null | undefined): 'up' | 'down' {
    const tip = svg?.querySelector('polyline')?.getAttribute('points')?.split(' ');
    if (!tip) throw new Error('arrow has no head');
    // points = "x y x y x y"; the tip is the middle vertex.
    return Number(tip[3]) < Number(tip[1]) ? 'up' : 'down';
  }

  it('draws a DOWN arrow for descending and an UP arrow for ascending on the trigger', () => {
    // Issue #747 §4.3 made it a real arrow (shaft + head); UX review item 19 fixed the
    // direction: descending = down arrow, ascending = up arrow.
    const { container: descContainer } = render(SortDropdown, {
      props: {
        sortOptions: gallerySortOptions,
        sortBy: 'upload_time',
        sortOrder: 'desc',
        ariaLabelKey: 'gallery.sort.label',
      },
    });
    const descArrow = descContainer.querySelector('.direction-arrow');
    expect(descArrow).not.toBeNull();
    expect(arrowPoints(descArrow)).toBe('down');
    // A real arrow has a shaft (line) as well as a head (polyline) — a bare
    // chevron has only the polyline.
    expect(descArrow?.querySelector('line')).not.toBeNull();

    const { container: ascContainer } = render(SortDropdown, {
      props: {
        sortOptions: gallerySortOptions,
        sortBy: 'upload_time',
        sortOrder: 'asc',
        ariaLabelKey: 'gallery.sort.label',
      },
    });
    expect(arrowPoints(ascContainer.querySelector('.direction-arrow'))).toBe('up');
  });

  it('always draws the trigger arrow downward for a noDirection (relevance) option', () => {
    // Relevance is fixed best-first, i.e. descending, whatever sortOrder says.
    const { container } = render(SortDropdown, {
      props: {
        sortOptions: searchSortOptions,
        sortBy: 'relevance',
        sortOrder: 'asc',
        ariaLabelKey: 'search.sort.label',
      },
    });

    expect(arrowPoints(container.querySelector('.direction-arrow'))).toBe('down');
  });

  it('gives only the active dropdown item an arrow, pointing the same way as the trigger', async () => {
    render(SortDropdown, {
      props: {
        sortOptions: gallerySortOptions,
        sortBy: 'filename',
        sortOrder: 'asc',
        ariaLabelKey: 'gallery.sort.label',
      },
    });

    await fireEvent.click(screen.getByRole('button', { name: 'gallery.sort.label' }));

    const activeItem = screen
      .getByText('gallery.sort.filename', { selector: '.option-label' })
      .closest('.dropdown-item');
    const optionArrow = activeItem?.querySelector('.option-arrow');
    expect(optionArrow).not.toBeNull();
    expect(arrowPoints(optionArrow)).toBe('up');

    // A non-active option renders no arrow at all.
    const inactiveItem = screen
      .getByText('gallery.sort.duration', { selector: '.option-label' })
      .closest('.dropdown-item');
    expect(inactiveItem?.querySelector('.option-arrow')).toBeNull();
  });

  it('points the active menu item down when the current order is descending', async () => {
    render(SortDropdown, {
      props: {
        sortOptions: gallerySortOptions,
        sortBy: 'filename',
        sortOrder: 'desc',
        ariaLabelKey: 'gallery.sort.label',
      },
    });

    await fireEvent.click(screen.getByRole('button', { name: 'gallery.sort.label' }));

    const activeItem = screen
      .getByText('gallery.sort.filename', { selector: '.option-label' })
      .closest('.dropdown-item');
    expect(arrowPoints(activeItem?.querySelector('.option-arrow'))).toBe('down');
  });

  it('applies the align-right class to the menu when align="right"', async () => {
    const { container } = render(SortDropdown, {
      props: {
        sortOptions: searchSortOptions,
        sortBy: 'relevance',
        sortOrder: 'desc',
        ariaLabelKey: 'search.sort.label',
        align: 'right',
      },
    });

    await fireEvent.click(screen.getByRole('button', { name: 'search.sort.label' }));
    expect(container.querySelector('.dropdown-menu.align-right')).not.toBeNull();
  });

  it('closes the menu on an outside click', async () => {
    const outside = document.createElement('button');
    document.body.appendChild(outside);

    render(SortDropdown, {
      props: {
        sortOptions: gallerySortOptions,
        sortBy: 'upload_time',
        sortOrder: 'desc',
        ariaLabelKey: 'gallery.sort.label',
      },
    });

    const trigger = screen.getByRole('button', { name: 'gallery.sort.label' });
    await fireEvent.click(trigger);
    expect(trigger).toHaveAttribute('aria-expanded', 'true');

    // Assert via `aria-expanded` (updates synchronously with `isOpen`) rather than
    // waiting for the menu's `scale` outro transition to finish removing it from the
    // DOM — the transition itself isn't what this test is about.
    await fireEvent.click(outside);
    expect(trigger).toHaveAttribute('aria-expanded', 'false');

    outside.remove();
  });
});
