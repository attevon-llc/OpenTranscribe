/**
 * `SearchResultCard.svelte` — the compact transcript hit (docs/design/search_results_ux_audit.md).
 *
 * DEFECTS THESE CATCH:
 * - The card used to print speakers, size, tags and three badge families all at once; the
 *   compact view must keep them behind "Details".
 * - The play control showed a red pause glyph that ignored the real player: a paused or
 *   finished clip still read as "playing", and re-clicking it closed the player instead of
 *   pausing. The control must follow `activePreview.playing` and toggle, not restart.
 */
import { describe, it, expect, vi } from 'vitest';
import { render, fireEvent, within } from '@testing-library/svelte';
import { readFileSync } from 'node:fs';
import { resolve } from 'node:path';

vi.mock('$stores/locale', async () => (await import('./searchTestFixtures')).enLocaleMock());
vi.mock('$lib/prefetch', () => ({ prefetchFileDetails: vi.fn(), cancelPrefetch: vi.fn() }));

import SearchResultCard from './SearchResultCard.svelte';
import { searchHit } from './searchTestFixtures';

describe('SearchResultCard — compact result', () => {
  it('shows the title link, a muted meta line and one snippet, but not speakers, size or tags', () => {
    const { getByRole, getByText, queryByText, container } = render(SearchResultCard, {
      props: { hit: searchHit() },
    });

    expect(getByRole('link', { name: /Roadmap review/ })).toHaveAttribute('href', '/files/file-1');
    expect(getByText('87% match')).toBeInTheDocument();
    expect(container.querySelectorAll('.occurrence-row')).toHaveLength(1);
    expect(queryByText('Bob')).toBeNull();
    expect(queryByText('planning')).toBeNull();
    expect(queryByText('50.0 MB')).toBeNull();
  });

  it('shows two snippets when the hit has both exact and related matches', () => {
    const { container } = render(SearchResultCard, {
      props: { hit: searchHit({ has_both_match_types: true }) },
    });
    expect(container.querySelectorAll('.occurrence-row')).toHaveLength(2);
  });

  it('"Show more matches" is a disclosure that reveals and re-hides the rest', async () => {
    const { getByRole, container } = render(SearchResultCard, { props: { hit: searchHit() } });
    const more = getByRole('button', { name: 'Show 2 more matches' });
    expect(more).toHaveAttribute('aria-expanded', 'false');
    expect(container.querySelector(`#${more.getAttribute('aria-controls')}`)).not.toBeNull();

    await fireEvent.click(more);
    expect(container.querySelectorAll('.occurrence-row')).toHaveLength(3);
    expect(getByRole('button', { name: 'Show less' })).toHaveAttribute('aria-expanded', 'true');

    await fireEvent.click(getByRole('button', { name: 'Show less' }));
    expect(container.querySelectorAll('.occurrence-row')).toHaveLength(1);
  });

  it('narrow layout: "Details" expands the info card inline', async () => {
    const { getByRole, getByText, container } = render(SearchResultCard, {
      props: { hit: searchHit(), panelMode: false },
    });
    const details = getByRole('button', { name: 'Details' });
    expect(details).toHaveAttribute('aria-expanded', 'false');
    expect(details).not.toHaveAttribute('aria-pressed');

    await fireEvent.click(details);

    expect(details).toHaveAttribute('aria-expanded', 'true');
    const region = container.querySelector(`#${details.getAttribute('aria-controls')}`);
    expect(region).not.toBeNull();
    expect(within(region as HTMLElement).getByText('Bob')).toBeInTheDocument();
    expect(getByText('planning')).toBeInTheDocument();
  });

  it('wide layout: "Details" pins the side panel instead of expanding inline', async () => {
    const toggleDetails = vi.fn();
    const { getByRole, queryByText } = render(SearchResultCard, {
      props: { hit: searchHit(), panelMode: true, selected: true, panelId: 'panel-x' },
      events: { toggleDetails },
    });
    const details = getByRole('button', { name: 'Details' });
    expect(details).toHaveAttribute('aria-pressed', 'true');
    expect(details).toHaveAttribute('aria-controls', 'panel-x');
    expect(details).not.toHaveAttribute('aria-expanded');

    await fireEvent.click(details);

    expect(toggleDetails).toHaveBeenCalledTimes(1);
    expect(toggleDetails.mock.calls[0][0].detail).toBe('file-1');
    expect(queryByText('Bob')).toBeNull();
  });

  it('pointer hover and keyboard focus both ask the page to show this hit', async () => {
    const inspect = vi.fn();
    const { container, getByRole } = render(SearchResultCard, {
      props: { hit: searchHit() },
      events: { inspect },
    });
    await fireEvent.mouseEnter(container.querySelector('article')!);
    await fireEvent.focusIn(getByRole('button', { name: 'Details' }));
    expect(inspect).toHaveBeenCalledTimes(2);
    expect(inspect.mock.calls.map((c) => c[0].detail)).toEqual(['file-1', 'file-1']);
  });
});

describe('SearchResultCard — play/pause control', () => {
  const playing = { fileUuid: 'file-1', startTime: 12, playing: true };

  it('idle: offers "Play from this point" and starts a preview of that occurrence', async () => {
    const preview = vi.fn();
    const togglePlayback = vi.fn();
    const { getByRole } = render(SearchResultCard, {
      props: { hit: searchHit() },
      events: { preview, togglePlayback },
    });
    const btn = getByRole('button', { name: 'Play from this point' });
    expect(btn).toHaveAttribute('aria-pressed', 'false');

    await fireEvent.click(btn);

    expect(togglePlayback).not.toHaveBeenCalled();
    expect(preview.mock.calls[0][0].detail).toMatchObject({ fileUuid: 'file-1', startTime: 12 });
  });

  it('playing: shows Pause (pressed) and toggles playback instead of restarting', async () => {
    const preview = vi.fn();
    const togglePlayback = vi.fn();
    const { getByRole } = render(SearchResultCard, {
      props: { hit: searchHit(), activePreview: playing },
      events: { preview, togglePlayback },
    });
    const btn = getByRole('button', { name: 'Pause' });
    expect(btn).toHaveAttribute('aria-pressed', 'true');

    await fireEvent.click(btn);

    expect(togglePlayback).toHaveBeenCalledTimes(1);
    expect(preview).not.toHaveBeenCalled();
  });

  it('follows the player: a pause reported by the player turns the control into Resume', async () => {
    const { getByRole, rerender } = render(SearchResultCard, {
      props: { hit: searchHit(), activePreview: playing },
    });
    expect(getByRole('button', { name: 'Pause' })).toBeInTheDocument();

    await rerender({ hit: searchHit(), activePreview: { ...playing, playing: false } });

    const btn = getByRole('button', { name: 'Resume playback' });
    expect(btn).toHaveAttribute('aria-pressed', 'false');
  });

  it('another result starting returns this control to idle', async () => {
    const { getByRole, rerender } = render(SearchResultCard, {
      props: { hit: searchHit(), activePreview: playing },
    });
    await rerender({
      hit: searchHit(),
      activePreview: { fileUuid: 'other-file', startTime: 12, playing: false },
    });
    expect(getByRole('button', { name: 'Play from this point' })).toBeInTheDocument();
  });
});

// Seen in the browser on /search (Vite dev): a clicked clip played while its control kept
// showing an idle "Play", because the template called `playbackState(occurrence)` and the
// page swaps `activePreview` with `hit` unchanged, so nothing the call listed had changed.
// jsdom re-renders it either way, so the behavioural tests above cannot see this; pin the
// template's dependency list at the source instead.
describe('SearchResultCard — playback state dependency', () => {
  it('passes activePreview into the template call that derives the control state', () => {
    const source = readFileSync(resolve(__dirname, 'SearchResultCard.svelte'), 'utf8');
    expect(source).toContain('state={playbackState(occurrence, activePreview)}');
    expect(source).not.toMatch(/playbackState\(occurrence\)/);
  });
});
