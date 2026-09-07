/**
 * The search page decides whether to show results or the "no search yet" empty
 * state from the `clear` event this component emits. Issue #742: the ✕ button
 * emitted it, but emptying the field with backspace did not — so the previous
 * results stayed on screen over an empty search box, with no way to get back to
 * the empty state except a reload.
 *
 * These pin both routes to empty and, just as importantly, that `clear` does NOT
 * fire while the user is still typing — over-firing would wipe results on every
 * keystroke.
 */
import { describe, it, expect, vi, beforeEach } from 'vitest';
import { render, fireEvent, waitFor } from '@testing-library/svelte';

vi.mock('$lib/axios', () => ({
  default: { get: vi.fn().mockResolvedValue({ data: [] }) },
}));

vi.mock('$app/navigation', () => ({ goto: vi.fn() }));

vi.mock('$stores/locale', () => ({
  t: {
    subscribe: (run: (value: (key: string) => string) => void) => {
      run((key: string) => key);
      return () => {};
    },
  },
}));

import SearchAutocomplete from './SearchAutocomplete.svelte';

function renderInput(value = '') {
  const onClear = vi.fn();
  const onSearch = vi.fn();
  render(SearchAutocomplete, {
    props: { value },
    events: { clear: onClear, search: onSearch },
  } as never);
  // `.search-input` is an E2E-guarded selector (backend/tests/e2e/test_search.py).
  const input = document.querySelector('.search-input') as HTMLInputElement;
  return { input, onClear, onSearch };
}

describe('SearchAutocomplete clear behaviour (#742)', () => {
  beforeEach(() => {
    vi.clearAllMocks();
  });

  it('emits clear when the field is emptied by typing (backspace to empty)', async () => {
    const { input, onClear } = renderInput('acme quarterly');

    await fireEvent.input(input, { target: { value: '' } });

    await waitFor(() => expect(onClear).toHaveBeenCalledTimes(1));
  });

  it('emits clear when the ✕ button is pressed', async () => {
    const { onClear } = renderInput('acme quarterly');

    // `.clear-btn` is an E2E-guarded selector; it fires on mousedown, not click.
    const clearBtn = document.querySelector('.clear-btn') as HTMLElement;
    expect(clearBtn).toBeTruthy();
    await fireEvent.mouseDown(clearBtn);

    await waitFor(() => expect(onClear).toHaveBeenCalledTimes(1));
  });

  it('does not emit clear while the user is still typing a non-empty query', async () => {
    const { input, onClear } = renderInput('');

    await fireEvent.input(input, { target: { value: 'a' } });
    await fireEvent.input(input, { target: { value: 'ac' } });
    await fireEvent.input(input, { target: { value: 'acme' } });

    expect(onClear).not.toHaveBeenCalled();
  });
});
