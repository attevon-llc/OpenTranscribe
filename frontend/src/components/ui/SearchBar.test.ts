import { describe, it, expect } from 'vitest';
import { render, screen, fireEvent } from '@testing-library/svelte';
import SearchBar from './SearchBar.svelte';

// Svelte 5 removed component.$on(...); these assert observable DOM/state, which is
// this repo's component-testing convention (see ui/CLAUDE.md). Event wiring is
// exercised by the consuming components' integration + manual verification.

describe('SearchBar', () => {
  it('renders an input with the placeholder and aria-label', () => {
    render(SearchBar, { props: { placeholder: 'Search settings', ariaLabel: 'Search settings' } });
    const input = screen.getByRole('textbox', { name: 'Search settings' });
    expect(input).toBeInTheDocument();
    expect(input).toHaveAttribute('placeholder', 'Search settings');
  });

  it('shows the clear button only when there is text, and clearing empties the input', async () => {
    render(SearchBar, { props: { value: '', debounceMs: 0 } });
    expect(screen.queryByRole('button', { name: 'Clear search' })).not.toBeInTheDocument();

    const input = screen.getByRole('textbox') as HTMLInputElement;
    await fireEvent.input(input, { target: { value: 'hello' } });
    const clearBtn = screen.getByRole('button', { name: 'Clear search' });
    await fireEvent.click(clearBtn);
    expect(input.value).toBe('');
  });

  it('renders the counter and prev/next navigation in find mode', () => {
    render(SearchBar, { props: { showNav: true, counterText: '2 of 17', value: 'the' } });
    expect(screen.getByText('2 of 17')).toBeInTheDocument();
    expect(screen.getByRole('button', { name: /next match/i })).toBeInTheDocument();
    expect(screen.getByRole('button', { name: /previous match/i })).toBeInTheDocument();
  });

  it('disables navigation when navDisabled is set', () => {
    render(SearchBar, { props: { showNav: true, navDisabled: true, value: 'x' } });
    expect(screen.getByRole('button', { name: /next match/i })).toBeDisabled();
    expect(screen.getByRole('button', { name: /previous match/i })).toBeDisabled();
  });

  it('styles the counter as no-results and shows a loading status', () => {
    const { rerender } = render(SearchBar, {
      props: { counterText: 'No matches', noResults: true, value: 'zzz' },
    });
    const counter = screen.getByText('No matches');
    expect(counter).toHaveClass('no-results');

    rerender({ loading: true, statusText: 'searching entire transcript…', value: 'zzz' });
    expect(screen.getByText('searching entire transcript…')).toBeInTheDocument();
  });

  it('exposes combobox wiring when role="combobox"', () => {
    render(SearchBar, {
      props: {
        role: 'combobox',
        ariaControls: 'results-list',
        ariaExpanded: true,
        inputId: 'settings-search-input',
      },
    });
    const input = screen.getByRole('combobox');
    expect(input).toHaveAttribute('aria-controls', 'results-list');
    expect(input).toHaveAttribute('aria-expanded', 'true');
    expect(input).toHaveAttribute('aria-autocomplete', 'list');
    expect(input).toHaveAttribute('id', 'settings-search-input');
  });
});
