import { describe, it, expect, vi, afterEach } from 'vitest';
import { render, fireEvent } from '@testing-library/svelte';

vi.mock('$stores/locale', () => ({
  t: {
    subscribe: (run: (value: (key: string, vars?: unknown) => string) => void) => {
      run((key: string) => key);
      return () => {};
    },
  },
}));

import SettingsSearch from './SettingsSearch.svelte';
import type { FuzzyIndex } from '$lib/search/fuzzyMatcher';
import type { SettingsSearchItem } from '$lib/search/settingsSearchIndex';

function item(label: string): SettingsSearchItem {
  return {
    sectionId: 'profile',
    sectionLabel: 'Profile',
    label,
    keywords: label,
    anchorText: label,
    isSectionTitle: false,
  };
}

// A deterministic stand-in: any query matches two rows, except the literal "nothing".
const index = {
  search: (query: string) =>
    query === 'nothing'
      ? []
      : [item('Profile photo'), item('Profile name')].map((it, refIndex) => ({
          item: it,
          score: 0,
          refIndex,
        })),
  setCollection: () => {},
} as unknown as FuzzyIndex<SettingsSearchItem>;

const input = (c: HTMLElement) => c.querySelector('input') as HTMLInputElement;
const type = (c: HTMLElement, value: string) => fireEvent.input(input(c), { target: { value } });
const clearButton = (c: HTMLElement) => c.querySelector('.search-bar-clear');

afterEach(() => vi.restoreAllMocks());

describe('SettingsSearch', () => {
  it('is a labelled search landmark around a native search input', () => {
    const { container } = render(SettingsSearch, { props: { index } });
    const landmark = container.querySelector('[role="search"]') as HTMLElement;
    expect(landmark.getAttribute('aria-label')).toBe('settingsSearch.ariaLabel');
    expect(landmark.contains(input(container))).toBe(true);
    expect(input(container).type).toBe('search');
    expect(input(container).getAttribute('autocomplete')).toBe('off');
    expect(input(container).getAttribute('spellcheck')).toBe('false');
    expect(input(container).getAttribute('enterkeyhint')).toBe('search');
  });

  it('shows the clear button only while there is text, and clears on click', async () => {
    const { container } = render(SettingsSearch, { props: { index } });
    expect(clearButton(container)).toBeNull();

    await type(container, 'prof');
    const button = clearButton(container) as HTMLButtonElement;
    expect(button).not.toBeNull();
    expect(button.getAttribute('aria-label')).toBe('search.clearSearch');
    expect(container.querySelector('[role="listbox"]')).not.toBeNull();

    await fireEvent.click(button);
    expect(input(container).value).toBe('');
    expect(clearButton(container)).toBeNull();
    expect(container.querySelector('[role="listbox"]')).toBeNull();
  });

  it('clears the text on the first Escape and leaves the second for the dialog', async () => {
    const { container } = render(SettingsSearch, { props: { index } });
    const reachedDialog = vi.fn();
    document.addEventListener('keydown', reachedDialog);
    try {
      await type(container, 'prof');

      await fireEvent.keyDown(input(container), { key: 'Escape' });
      expect(input(container).value).toBe('');
      expect(reachedDialog).not.toHaveBeenCalled();

      await fireEvent.keyDown(input(container), { key: 'Escape' });
      expect(reachedDialog).toHaveBeenCalledOnce();
    } finally {
      document.removeEventListener('keydown', reachedDialog);
    }
  });

  it('announces the result count politely and keeps arrow keys on the active descendant', async () => {
    const { container } = render(SettingsSearch, {
      props: { index, idPrefix: 'spec' },
    });
    await type(container, 'prof');

    const live = container.querySelector('[role="status"]') as HTMLElement;
    expect(live.getAttribute('aria-live')).toBe('polite');
    expect(live.textContent).toContain('settingsSearch.resultsCount');
    expect(input(container).getAttribute('aria-activedescendant')).toBe('spec-result-0');

    await fireEvent.keyDown(input(container), { key: 'ArrowDown' });
    expect(input(container).getAttribute('aria-activedescendant')).toBe('spec-result-1');
    await fireEvent.keyDown(input(container), { key: 'ArrowUp' });
    expect(input(container).getAttribute('aria-activedescendant')).toBe('spec-result-0');
  });

  it('announces an empty result set instead of staying silent', async () => {
    const { container } = render(SettingsSearch, { props: { index } });
    await type(container, 'nothing');
    expect((container.querySelector('[role="status"]') as HTMLElement).textContent).toContain(
      'settingsSearch.noResultsTitle'
    );
  });
});
