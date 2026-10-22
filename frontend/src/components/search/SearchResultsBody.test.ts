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

import SearchResultsBody from './SearchResultsBody.svelte';
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
    const pinA = within(cards[0]).getByRole('button', { name: 'Details' });

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
