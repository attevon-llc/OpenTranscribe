import { describe, it, expect, vi, beforeEach, afterEach } from 'vitest';
import { render, screen, fireEvent } from '@testing-library/svelte';
import SearchableSelect from './SearchableSelect.svelte';

interface Item {
  id: number;
  name: string;
}

const items: Item[] = [
  { id: 1, name: 'Alpha' },
  { id: 2, name: 'Beta' },
];

// Typed as `(item: unknown)` so it's assignable to the component's prop type:
// when rendering via `render(Component, { props })`, the generic `T` widens to
// `unknown`, so the prop callbacks must accept `unknown` and narrow internally.
const getLabel = (item: unknown) => (item as Item).name;

describe('SearchableSelect', () => {
  beforeEach(() => {
    vi.useFakeTimers();
  });
  afterEach(() => {
    vi.runOnlyPendingTimers();
    vi.useRealTimers();
  });

  it('debounces the query and calls fetchFn, then renders results', async () => {
    const fetchFn = vi.fn(async () => items);
    render(SearchableSelect, {
      props: { fetchFn, getLabel, debounceMs: 250, minChars: 1 },
    });

    const input = screen.getByRole('combobox');
    await fireEvent.input(input, { target: { value: 'al' } });

    // Not called before debounce elapses.
    expect(fetchFn).not.toHaveBeenCalled();

    await vi.advanceTimersByTimeAsync(250);
    expect(fetchFn).toHaveBeenCalledWith('al');

    expect(screen.getByRole('option', { name: 'Alpha' })).toBeInTheDocument();
    expect(screen.getByRole('option', { name: 'Beta' })).toBeInTheDocument();
  });

  it('dispatches `select` when an option is clicked', async () => {
    const fetchFn = vi.fn(async () => items);
    const onSelect = vi.fn();
    render(SearchableSelect, {
      props: { fetchFn, getLabel, debounceMs: 250 },
      events: { select: onSelect },
    });

    await fireEvent.input(screen.getByRole('combobox'), { target: { value: 'b' } });
    await vi.advanceTimersByTimeAsync(250);

    await fireEvent.click(screen.getByRole('option', { name: 'Beta' }));
    expect(onSelect).toHaveBeenCalledTimes(1);
    expect((onSelect.mock.calls[0][0] as CustomEvent).detail).toEqual({ id: 2, name: 'Beta' });
  });

  it('selects the highlighted item with arrow keys + Enter', async () => {
    const fetchFn = vi.fn(async () => items);
    const onSelect = vi.fn();
    render(SearchableSelect, {
      props: { fetchFn, getLabel, debounceMs: 250 },
      events: { select: onSelect },
    });

    const input = screen.getByRole('combobox');
    await fireEvent.input(input, { target: { value: 'a' } });
    await vi.advanceTimersByTimeAsync(250);

    // Highlight defaults to index 0; move down to index 1, then select.
    await fireEvent.keyDown(input, { key: 'ArrowDown' });
    await fireEvent.keyDown(input, { key: 'Enter' });
    expect((onSelect.mock.calls[0][0] as CustomEvent).detail).toEqual({ id: 2, name: 'Beta' });
  });

  it('closes the dropdown on Escape', async () => {
    const fetchFn = vi.fn(async () => items);
    render(SearchableSelect, { props: { fetchFn, getLabel, debounceMs: 250 } });

    const input = screen.getByRole('combobox');
    await fireEvent.input(input, { target: { value: 'a' } });
    await vi.advanceTimersByTimeAsync(250);
    expect(screen.getByRole('listbox')).toBeInTheDocument();

    await fireEvent.keyDown(input, { key: 'Escape' });
    expect(screen.queryByRole('listbox')).toBeNull();
  });

  it('clears the pending debounce timer on destroy, so fetchFn never fires after unmount', async () => {
    const fetchFn = vi.fn(async () => items);
    const { unmount } = render(SearchableSelect, {
      props: { fetchFn, getLabel, debounceMs: 250 },
    });

    const input = screen.getByRole('combobox');
    await fireEvent.input(input, { target: { value: 'a' } });
    // Debounce timer is pending — destroy before it fires.
    unmount();

    await vi.advanceTimersByTimeAsync(250);
    expect(fetchFn).not.toHaveBeenCalled();
  });
});
