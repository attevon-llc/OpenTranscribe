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
