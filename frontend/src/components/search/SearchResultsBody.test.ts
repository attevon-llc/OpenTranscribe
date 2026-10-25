/**
 * `SearchResultsBody.svelte` — the collapsible Summaries/Transcripts sections and the
 * knowledge-panel aside on `/search` (docs/design/search_results_ux_audit.md).
 *
 * DEFECTS THESE CATCH:
 * - The sections could not be collapsed, so many summary hits buried every transcript hit.
 * - The aside must never land inside `.results-list`: `test_search.py` counts
 *   `.results-list > *` as transcript hits.
 * - A pinned result must survive hovering a different one, and unpin on a second press.
 */
import { describe, it, expect, vi } from 'vitest';
import { render, fireEvent, within } from '@testing-library/svelte';

vi.mock('$stores/locale', async () => (await import('./searchTestFixtures')).enLocaleMock());
vi.mock('$lib/prefetch', () => ({ prefetchFileDetails: vi.fn(), cancelPrefetch: vi.fn() }));
vi.mock('$components/RetrievalQualityNotice.svelte', () => ({ default: () => {} }));
// jsdom's animate() stub never fires `finish`, so a collapsing section's slide outro would
// keep the content mounted forever; a zero-length transition completes synchronously.
vi.mock('svelte/transition', async (importOriginal) => ({
  ...(await importOriginal<typeof import('svelte/transition')>()),
  slide: () => ({ duration: 0 }),
}));

import SearchResultsBody, { DEFAULT_OPEN_RESULTS } from './SearchResultsBody.svelte';
import { searchHit } from './searchTestFixtures';
import type { SummaryHit } from '$stores/search';

const hits = [
  searchHit({ file_uuid: 'a', title: 'Alpha meeting', speakers: ['Ann'] }),
  searchHit({ file_uuid: 'b', title: 'Beta meeting', speakers: ['Ben'] }),
];
const summaryHits: SummaryHit[] = [
  {
    file_uuid: 's1',
    file_id: 9,
    title: 'Summary file',
    matches: [{ key_path: 'bluf', snippet: 'Ship it.' }],
  },
];

function props(overrides: Record<string, unknown> = {}) {
  return {
    query: 'ship',
    searchMode: 'keyword',
    showSummarySection: true,
    showTranscriptSection: true,
    results: hits,
    summaryResults: summaryHits,
    summaryTotal: 1,
    summaryUnavailable: null,
    activePreview: null,
    panelMode: true,
    ...overrides,
  };
}

function aside(container: HTMLElement) {
  return container.querySelector('aside.result-info-aside') as HTMLElement | null;
}

describe('SearchResultsBody — collapsible sections', () => {
  it('renders each section title as an h2 disclosure, open by default', () => {
    const { getByRole } = render(SearchResultsBody, { props: props() });
    for (const name of ['Summaries', 'Transcripts']) {
      const btn = getByRole('button', { name });
      expect(btn).toHaveAttribute('aria-expanded', 'true');
      expect(btn.closest('h2')).not.toBeNull();
    }
  });

  it('collapsing Summaries hides its hits and leaves the transcript hits', async () => {
    const { getByRole, queryByText, container } = render(SearchResultsBody, { props: props() });

    await fireEvent.click(getByRole('button', { name: 'Summaries' }));

    expect(getByRole('button', { name: 'Summaries' })).toHaveAttribute('aria-expanded', 'false');
    expect(queryByText('Summary file')).toBeNull();
    expect(container.querySelectorAll('.results-list > .result-card')).toHaveLength(2);
  });

  it('collapsing Transcripts hides the hits and the info panel', async () => {
    const { getByRole, container } = render(SearchResultsBody, { props: props() });

    await fireEvent.click(getByRole('button', { name: 'Transcripts' }));

    expect(container.querySelector('.results-list')).toBeNull();
    expect(aside(container)).toBeNull();
  });

  it('with only transcripts selected there is no section toggle to collapse', () => {
    const { queryByRole, container } = render(SearchResultsBody, {
      props: props({ showSummarySection: false }),
    });
    expect(queryByRole('button', { name: 'Transcripts' })).toBeNull();
    expect(container.querySelectorAll('.results-list > .result-card')).toHaveLength(2);
  });
});

describe('SearchResultsBody — info panel', () => {
  it('sits outside .results-list, which still holds only the result cards', () => {
    const { container } = render(SearchResultsBody, { props: props() });
    const panel = aside(container);
    expect(panel).not.toBeNull();
    expect(panel!.closest('.results-list')).toBeNull();
    expect(container.querySelectorAll('.results-list > *')).toHaveLength(2);
  });

  it('defaults to the top result and follows the hovered one', async () => {
    const { container } = render(SearchResultsBody, { props: props() });
    expect(within(aside(container)!).getByText('Ann')).toBeInTheDocument();

    const cards = container.querySelectorAll('.results-list > .result-card');
    await fireEvent.mouseEnter(cards[1]);

    expect(within(aside(container)!).getByText('Ben')).toBeInTheDocument();
    expect(within(aside(container)!).queryByText('Ann')).toBeNull();
  });

  it('a pinned result stays in the panel while another is hovered, until unpinned', async () => {
    const { container } = render(SearchResultsBody, { props: props() });
    const cards = container.querySelectorAll<HTMLElement>('.results-list > .result-card');
    const pinA = within(cards[0]).getByRole('button', { name: 'Pin details' });

    await fireEvent.click(pinA);
    expect(pinA).toHaveAttribute('aria-pressed', 'true');
    await fireEvent.mouseEnter(cards[1]);
    expect(within(aside(container)!).getByText('Ann')).toBeInTheDocument();

    await fireEvent.click(pinA);
    expect(pinA).toHaveAttribute('aria-pressed', 'false');
    await fireEvent.mouseEnter(cards[1]);
    expect(within(aside(container)!).getByText('Ben')).toBeInTheDocument();
  });

  it('narrow layout renders no aside; Details expands inline instead', async () => {
    const { container } = render(SearchResultsBody, { props: props({ panelMode: false }) });
    expect(aside(container)).toBeNull();

    const card = container.querySelector<HTMLElement>('.results-list > .result-card')!;
    const details = within(card).getByRole('button', { name: 'Details' });
    await fireEvent.click(details);

    expect(details).toHaveAttribute('aria-expanded', 'true');
    expect(within(card).getByText('Ann')).toBeInTheDocument();
  });
});

describe('SearchResultsBody — pinning is visible', () => {
  // Owner report: "Details" did nothing on transcript results. In the wide layout the panel
  // already showed the hovered card, so pinning it changed nothing that could be seen.
  it('pinning the hit the panel already shows marks the panel Pinned, with an Unpin control', async () => {
    const { container } = render(SearchResultsBody, { props: props() });
    const panel = () => within(aside(container)!);
    expect(panel().queryByText('Pinned')).toBeNull();

    const card = container.querySelector<HTMLElement>('.results-list > .result-card')!;
    await fireEvent.mouseEnter(card);
    await fireEvent.click(within(card).getByRole('button', { name: 'Pin details' }));

    expect(panel().getByText('Pinned')).toBeInTheDocument();
    expect(aside(container)!.querySelector('.info-card.pinned')).not.toBeNull();
    expect(card).toHaveClass('selected');
  });

  it("the panel's Unpin releases the pin: the card unpresses and hover drives the panel again", async () => {
    const { container } = render(SearchResultsBody, { props: props() });
    const cards = container.querySelectorAll<HTMLElement>('.results-list > .result-card');
    const pinA = within(cards[0]).getByRole('button', { name: 'Pin details' });
    await fireEvent.click(pinA);

    await fireEvent.click(within(aside(container)!).getByRole('button', { name: 'Unpin details' }));

    expect(pinA).toHaveAttribute('aria-pressed', 'false');
    expect(within(aside(container)!).queryByText('Pinned')).toBeNull();
    await fireEvent.mouseEnter(cards[1]);
    expect(within(aside(container)!).getByText('Ben')).toBeInTheDocument();
  });
});

describe('SearchResultsBody — collapsible match lists', () => {
  const many = Array.from({ length: DEFAULT_OPEN_RESULTS + 2 }, (_, i) =>
    searchHit({ file_uuid: `h${i}`, title: `Hit ${i}` })
  );

  function carets(container: HTMLElement) {
    return Array.from(
      container.querySelectorAll<HTMLElement>('.results-list > .result-card .matches-toggle')
    );
  }

  it('opens the first results and collapses the rest by default', () => {
    const { container } = render(SearchResultsBody, { props: props({ results: many }) });
    const states = carets(container).map((c) => c.getAttribute('aria-expanded'));
    expect(states).toEqual([...Array(DEFAULT_OPEN_RESULTS).fill('true'), 'false', 'false']);
  });

  it('a remembered choice beats the position default, and a caret asks to flip it', async () => {
    const setMatchListsOpen = vi.fn();
    const { container } = render(SearchResultsBody, {
      props: props({ results: many, matchListOpen: { h0: false, h6: true } }),
      events: { setMatchListsOpen },
    });
    const all = carets(container);
    expect(all[0]).toHaveAttribute('aria-expanded', 'false');
    expect(all[6]).toHaveAttribute('aria-expanded', 'true');

    await fireEvent.click(all[1]);

    expect(setMatchListsOpen.mock.calls[0][0].detail).toEqual({ fileUuids: ['h1'], open: false });
  });

  it('Collapse all / Expand all sits outside .results-list and sets every card on the page', async () => {
    const setMatchListsOpen = vi.fn();
    const { container, getByRole, rerender } = render(SearchResultsBody, {
      props: props({ results: many }),
      events: { setMatchListsOpen },
    });
    const collapseAll = getByRole('button', { name: 'Collapse all' });
    expect(collapseAll.closest('.results-list')).toBeNull();
    expect(container.querySelectorAll('.results-list > *')).toHaveLength(many.length);

    await fireEvent.click(collapseAll);
    expect(setMatchListsOpen.mock.calls[0][0].detail).toEqual({
      fileUuids: many.map((h) => h.file_uuid),
      open: false,
    });

    const allClosed = Object.fromEntries(many.map((h) => [h.file_uuid, false]));
    await rerender(props({ results: many, matchListOpen: allClosed }));
    await fireEvent.click(getByRole('button', { name: 'Expand all' }));
    expect(setMatchListsOpen.mock.calls[1][0].detail.open).toBe(true);
  });

  it('a single result has no collapse-all control', () => {
    const { queryByRole } = render(SearchResultsBody, {
      props: props({ results: [hits[0]] }),
    });
    expect(queryByRole('button', { name: 'Collapse all' })).toBeNull();
  });
});

describe('SearchResultsBody — chip filters reach the page', () => {
  it('a tag or collection chip in the panel is forwarded as filterTag / filterCollection', async () => {
    const filterTag = vi.fn();
    const filterCollection = vi.fn();
    const { container } = render(SearchResultsBody, {
      props: props(),
      events: { filterTag, filterCollection },
    });
    const panel = within(aside(container)!);

    await fireEvent.click(panel.getByRole('link', { name: 'planning' }));
    await fireEvent.click(panel.getByRole('link', { name: 'Q4 board' }));

    expect(filterTag.mock.calls[0][0].detail).toBe('planning');
    expect(filterCollection.mock.calls[0][0].detail).toBe('coll-1');
  });
});
