/**
 * The retrieval-quality notice (issue #461).
 *
 * This notice exists to carry a SPECIFIC measured finding, not a generic "beta"
 * label, so the two things worth pinning are (a) that it links to the issue
 * holding the actual numbers and (b) that each surface says the true thing about
 * ITSELF. The cross-encoder reranker runs on the chat path only —
 * `services/chat/retrieval.py` is its single production call site — while
 * `/search` is ranked entirely by OpenSearch RRF fusion. A component that
 * ignored `surface` and printed one blended sentence would look completely fine
 * on screen while telling search users their results are reranked, which is
 * false. Hence the "the two surfaces do not say the same thing" assertion.
 *
 * Dismissal is keyed PER SURFACE on purpose: the two notices report different
 * facts, so reading and dismissing one is not consent to hide the other.
 *
 * `$t` returns the raw key when i18next is uninitialised (see `$stores/locale`),
 * which is exactly what makes the key wiring assertable here.
 */
import { afterEach, beforeEach, describe, expect, it } from 'vitest';
import { fireEvent, render } from '@testing-library/svelte';

import RetrievalQualityNotice from './RetrievalQualityNotice.svelte';

const ISSUE_URL = 'https://github.com/attevon-llc/OpenTranscribe/issues/461';
const SPEAKERS_ISSUE_URL = 'https://github.com/attevon-llc/OpenTranscribe/issues/524';

beforeEach(() => localStorage.clear());
afterEach(() => localStorage.clear());

describe('RetrievalQualityNotice', () => {
  it('links to the issue holding the measurements, safely', () => {
    const { getByTestId } = render(RetrievalQualityNotice, { props: { surface: 'chat' } });

    const link = getByTestId('retrieval-quality-link') as HTMLAnchorElement;
    expect(link.getAttribute('href')).toBe(ISSUE_URL);
    // Opening in a new tab without `noopener` hands the issue page a window
    // handle back into the app.
    expect(link.getAttribute('rel')).toContain('noopener');
  });

  it('says something different on search than on chat', () => {
    // Both are mounted into the same document, so each is read from its OWN
    // container rather than through a body-wide testid query.
    const chatText =
      render(RetrievalQualityNotice, { props: { surface: 'chat' } }).container.textContent ?? '';
    const searchText =
      render(RetrievalQualityNotice, { props: { surface: 'search' } }).container.textContent ?? '';

    expect(chatText).toContain('retrievalQuality.chatMessage');
    expect(searchText).toContain('retrievalQuality.searchMessage');
    expect(searchText).not.toContain('retrievalQuality.chatMessage');
  });

  it('says the SPEAKERS thing on the speakers surface — issue #756', () => {
    // #756: `surface`'s copy selector used to be an `else -> chat` ternary, so widening the
    // union to include 'speakers' without also fixing the selector would render the CHAT
    // copy (which names the reranker — a mechanism that does not run on this page) with a
    // green test suite, because nothing enumerated the union. This is that enumeration.
    const { container, getByTestId } = render(RetrievalQualityNotice, {
      props: { surface: 'speakers' },
    });

    expect(container.textContent).toContain('retrievalQuality.speakersMessage');
    expect(container.textContent).not.toContain('retrievalQuality.chatMessage');
    expect(container.textContent).not.toContain('retrievalQuality.searchMessage');

    // The speakers surface also has its OWN issue link — #524 (the speaker axis at corpus
    // scale), never the RAG-retrieval #461 the other two surfaces point at.
    const link = getByTestId('retrieval-quality-link') as HTMLAnchorElement;
    expect(link.getAttribute('href')).toBe(SPEAKERS_ISSUE_URL);
  });

  it('dismisses the speakers surface independently of chat/search', () => {
    localStorage.setItem('opentr:retrievalQualityNotice:chat', 'dismissed');
    localStorage.setItem('opentr:retrievalQualityNotice:search', 'dismissed');

    const speakers = render(RetrievalQualityNotice, { props: { surface: 'speakers' } });
    expect(
      speakers.container.querySelector('[data-testid="retrieval-quality-notice"]')
    ).not.toBeNull();

    fireEvent.click(speakers.getByTestId('retrieval-quality-dismiss'));
    expect(speakers.queryByTestId('retrieval-quality-notice')).toBeNull();
    expect(localStorage.getItem('opentr:retrievalQualityNotice:speakers')).toBe('dismissed');
    // Dismissing speakers must not touch the other two surfaces' keys.
    expect(localStorage.getItem('opentr:retrievalQualityNotice:chat')).toBe('dismissed');
    expect(localStorage.getItem('opentr:retrievalQualityNotice:search')).toBe('dismissed');
  });

  it('hides itself when dismissed and remembers that', () => {
    const { getByTestId, queryByTestId } = render(RetrievalQualityNotice, {
      props: { surface: 'search' },
    });

    fireEvent.click(getByTestId('retrieval-quality-dismiss'));

    expect(queryByTestId('retrieval-quality-notice')).toBeNull();
    expect(localStorage.getItem('opentr:retrievalQualityNotice:search')).toBe('dismissed');
  });

  it('stays dismissed on the surface it was dismissed on, and only that one', () => {
    localStorage.setItem('opentr:retrievalQualityNotice:chat', 'dismissed');

    const chat = render(RetrievalQualityNotice, { props: { surface: 'chat' } });
    expect(chat.container.querySelector('[data-testid="retrieval-quality-notice"]')).toBeNull();

    const search = render(RetrievalQualityNotice, { props: { surface: 'search' } });
    expect(
      search.container.querySelector('[data-testid="retrieval-quality-notice"]')
    ).not.toBeNull();
  });
});
