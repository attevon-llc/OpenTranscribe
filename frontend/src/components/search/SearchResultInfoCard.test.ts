/**
 * `SearchResultInfoCard.svelte` — the knowledge-panel facts for one transcript hit.
 *
 * DEFECTS THESE CATCH: an unbounded speaker list (10+ names) blowing up the panel, the
 * internal `semantic` match source leaking into "Matched in", and the inline variant
 * repeating the title the card above it already shows.
 */
import { describe, it, expect, vi } from 'vitest';
import { render, fireEvent } from '@testing-library/svelte';

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

describe('SearchResultInfoCard — tags and collections', () => {
  it('lists the collections the API sent, each a link to the search filtered by it', () => {
    const { getByRole } = render(SearchResultInfoCard, {
      props: { hit: searchHit(), query: 'ship it' },
    });
    const link = getByRole('link', { name: 'Q4 board' });
    expect(link).toHaveAttribute('href', '/search?q=ship%20it&collection=coll-1');
    expect(link).toHaveAttribute('title', 'Show results in Q4 board');
    expect(getByRole('heading', { level: 4, name: 'Collections' })).toBeInTheDocument();
  });

  it('renders tags as links to the tag-filtered search', () => {
    const { getByRole } = render(SearchResultInfoCard, {
      props: { hit: searchHit(), query: 'ship' },
    });
    expect(getByRole('link', { name: 'planning' })).toHaveAttribute(
      'href',
      '/search?q=ship&tags=planning'
    );
  });

  it('a plain click filters in place; a modified click is left to the browser', async () => {
    const filterTag = vi.fn();
    const filterCollection = vi.fn();
    const { getByRole } = render(SearchResultInfoCard, {
      props: { hit: searchHit(), query: 'ship' },
      events: { filterTag, filterCollection },
    });

    const plainTag = await fireEvent.click(getByRole('link', { name: 'planning' }));
    await fireEvent.click(getByRole('link', { name: 'Q4 board' }));
    const newTab = await fireEvent.click(getByRole('link', { name: 'Q4 board' }), {
      ctrlKey: true,
    });

    expect(plainTag).toBe(false);
    expect(newTab).toBe(true);
    expect(filterTag.mock.calls.map((c) => c[0].detail)).toEqual(['planning']);
    expect(filterCollection.mock.calls.map((c) => c[0].detail)).toEqual(['coll-1']);
  });

  it('no collections, no Collections group', () => {
    const { queryByRole } = render(SearchResultInfoCard, {
      props: { hit: searchHit({ collections: [] }) },
    });
    expect(queryByRole('heading', { level: 4, name: 'Collections' })).toBeNull();
  });
});

describe('SearchResultInfoCard — pinned panel', () => {
  it('pinned: a Pinned label and an Unpin control that dispatches unpin', async () => {
    const unpin = vi.fn();
    const { getByRole, getByText } = render(SearchResultInfoCard, {
      props: { hit: searchHit(), variant: 'panel', pinned: true },
      events: { unpin },
    });
    expect(getByText('Pinned')).toBeInTheDocument();

    await fireEvent.click(getByRole('button', { name: 'Unpin details' }));

    expect(unpin).toHaveBeenCalledTimes(1);
    expect(unpin.mock.calls[0][0].detail).toBe('file-1');
  });

  it('not pinned, or inline: no pinned header', () => {
    const panel = render(SearchResultInfoCard, { props: { hit: searchHit(), variant: 'panel' } });
    expect(panel.queryByRole('button', { name: 'Unpin details' })).toBeNull();
    panel.unmount();
    const inline = render(SearchResultInfoCard, {
      props: { hit: searchHit(), variant: 'inline', pinned: true },
    });
    expect(inline.queryByRole('button', { name: 'Unpin details' })).toBeNull();
  });

  it('isolates numeric values so "50.0 MB" keeps its order in the RTL UI', () => {
    const { getByText } = render(SearchResultInfoCard, { props: { hit: searchHit() } });
    expect(getByText('50.0 MB').tagName).toBe('BDI');
  });
});
