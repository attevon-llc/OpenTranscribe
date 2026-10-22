/**
 * `SearchResultInfoCard.svelte` — the knowledge-panel facts for one transcript hit.
 *
 * DEFECTS THESE CATCH: an unbounded speaker list (10+ names) blowing up the panel, the
 * internal `semantic` match source leaking into "Matched in", and the inline variant
 * repeating the title the card above it already shows.
 */
import { describe, it, expect, vi } from 'vitest';
import { render } from '@testing-library/svelte';

vi.mock('$stores/locale', async () => (await import('./searchTestFixtures')).enLocaleMock());

import SearchResultInfoCard from './SearchResultInfoCard.svelte';
import { searchHit } from './searchTestFixtures';

describe('SearchResultInfoCard', () => {
  it('panel: title, facts and an Open file link', () => {
    const { getByRole, getByText } = render(SearchResultInfoCard, {
      props: { hit: searchHit(), variant: 'panel' },
    });
    expect(getByRole('heading', { level: 3, name: 'Roadmap review' })).toBeInTheDocument();
    expect(getByText('1:02:05')).toBeInTheDocument();
    expect(getByText('50.0 MB')).toBeInTheDocument();
    expect(getByRole('link', { name: 'Open file' })).toHaveAttribute('href', '/files/file-1');
  });

  it('caps speakers at 8 and summarises the rest', () => {
    const speakers = Array.from({ length: 11 }, (_, i) => `Speaker ${i + 1}`);
    const { getByText, queryByText } = render(SearchResultInfoCard, {
      props: { hit: searchHit({ speakers }) },
    });
    expect(getByText('Speaker 8')).toBeInTheDocument();
    expect(queryByText('Speaker 9')).toBeNull();
    expect(getByText('+3 more')).toBeInTheDocument();
  });

  it('lists match sources by label and drops the internal "semantic" source', () => {
    const { getByText, queryByText } = render(SearchResultInfoCard, {
      props: { hit: searchHit({ match_sources: ['title', 'semantic', 'speaker'] }) },
    });
    expect(getByText('Title, Speaker')).toBeInTheDocument();
    expect(queryByText(/semantic,/)).toBeNull();
  });

  it('inline: no title heading and no Open file link (the card already has both)', () => {
    const { queryByRole, getByText } = render(SearchResultInfoCard, {
      props: { hit: searchHit(), variant: 'inline' },
    });
    expect(queryByRole('heading', { level: 3 })).toBeNull();
    expect(queryByRole('link', { name: 'Open file' })).toBeNull();
    expect(getByText('planning')).toBeInTheDocument();
  });
});
