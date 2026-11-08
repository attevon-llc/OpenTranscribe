/**
 * The language fact is named in the interface language, not the browser's: with a German
 * interface on an English browser it read "English" beside German labels.
 */
import { describe, it, expect, vi } from 'vitest';
import { render } from '@testing-library/svelte';

vi.mock('$stores/locale', async () => {
  const { readable } = await import('svelte/store');
  return {
    locale: readable('de'),
    t: readable((key: string) => key),
  };
});

import SearchResultInfoCard from './SearchResultInfoCard.svelte';
import { searchHit } from './searchTestFixtures';

describe('SearchResultInfoCard language name', () => {
  it('uses the interface language, not navigator.language', () => {
    expect(navigator.language).toMatch(/^en/);
    const { getByText, queryByText } = render(SearchResultInfoCard, {
      props: { hit: searchHit({ language: 'en' }) },
    });
    expect(getByText('Englisch')).toBeInTheDocument();
    expect(queryByText('English')).toBeNull();
  });
});
