/**
 * The administrator's file picker (legal hold, quarantine): type a title or file name, pick
 * from what the gallery's own list endpoint returns, see the picks as removable chips. Nobody
 * types a file ID.
 */
import { describe, it, expect, vi, beforeEach } from 'vitest';
import { render, screen, fireEvent } from '@testing-library/svelte';
import type { AdminFileHit } from '$lib/api/fileSearch';

vi.mock('$stores/locale', async () => {
  const mocks = await import('$testMocks/galleryToolbarMocks');
  const { readable } = await import('svelte/store');
  return { ...mocks.englishLocale(), locale: readable('en') };
});
const api = vi.hoisted(() => ({ searchAdminFiles: vi.fn() }));
vi.mock('$lib/api/fileSearch', () => api);

import FileSearchPicker from './FileSearchPicker.svelte';

function media(
  uuid: string,
  title: string,
  filename = `${title}.mp4`,
  over: Partial<AdminFileHit> = {}
): AdminFileHit {
  return {
    uuid,
    title,
    filename,
    upload_time: '2026-01-01T12:00:00Z',
    owner: 'Alice Member',
    organization_name: 'Acme Legal',
    availability: 'available',
    hold: { held: false, held_by: [], hold_uuids: [] },
    ...over,
  };
}

const BOARD = media('11111111-1111-1111-1111-111111111111', 'Board meeting', 'board.mp4');
const BUDGET = media('22222222-2222-2222-2222-222222222222', 'Budget review', 'budget.mp4');

beforeEach(() => {
  vi.clearAllMocks();
  api.searchAdminFiles.mockResolvedValue([BOARD, BUDGET]);
});

async function typeAndPick(query: string, option: RegExp) {
  await fireEvent.input(screen.getByRole('combobox'), { target: { value: query } });
  await fireEvent.click(await screen.findByRole('option', { name: option }));
}

describe('FileSearchPicker', () => {
  it('searches by what was typed and adds the picked file as a chip, never asking for an ID', async () => {
    const changed = vi.fn();
    render(FileSearchPicker, {
      props: { scope: 'organization', label: 'Files to hold' },
      events: { change: (e: CustomEvent<AdminFileHit[]>) => changed(e.detail) },
    } as never);

    // The field is labelled and searches by title or file name.
    const input = screen.getByRole('combobox', { name: 'Files to hold' });
    expect(input).toHaveAttribute('placeholder', 'Search by title or file name');

    await typeAndPick('board', /^Board meeting · board\.mp4 · Alice Member · Acme Legal · /);

    expect(api.searchAdminFiles).toHaveBeenCalledWith('organization', 'board');
    expect(changed).toHaveBeenLastCalledWith([BOARD]);
    expect(screen.getByTestId('file-search-picker-chips')).toHaveTextContent('Board meeting');
    expect(screen.getByText('Selected: 1')).toBeInTheDocument();
    expect(screen.queryByText(/file id/i)).toBeNull();
  });

  it('does not offer a file that is already selected', async () => {
    render(FileSearchPicker, { props: { scope: 'platform', selected: [BOARD] } } as never);
    await fireEvent.input(screen.getByRole('combobox'), { target: { value: 'b' } });
    expect(await screen.findByRole('option', { name: /Budget review/ })).toBeInTheDocument();
    expect(screen.queryByRole('option', { name: /Board meeting/ })).toBeNull();
  });

  it('removes one chip, or clears them all', async () => {
    const changed = vi.fn();
    render(FileSearchPicker, {
      props: { scope: 'platform', selected: [BOARD, BUDGET] },
      events: { change: (e: CustomEvent<AdminFileHit[]>) => changed(e.detail) },
    } as never);

    await fireEvent.click(screen.getByRole('button', { name: 'Remove Board meeting' }));
    expect(changed).toHaveBeenLastCalledWith([BUDGET]);

    await fireEvent.click(screen.getByRole('button', { name: 'Clear selection' }));
    expect(changed).toHaveBeenLastCalledWith([]);
    expect(screen.queryByTestId('file-search-picker-chips')).toBeNull();
  });

  it('replaces the search with a note once the limit is reached', () => {
    render(FileSearchPicker, {
      props: { scope: 'platform', selected: [BOARD, BUDGET], max: 2 },
    } as never);
    expect(screen.queryByRole('combobox')).toBeNull();
    expect(screen.getByRole('status')).toHaveTextContent('The limit of 2 files is reached.');
  });

  it('while the parent submits, shows the chips but allows no change', () => {
    render(FileSearchPicker, {
      props: { scope: 'platform', selected: [BOARD], disabled: true },
    } as never);
    expect(screen.getByTestId('file-search-picker-chips')).toHaveTextContent('Board meeting');
    expect(screen.queryByRole('combobox')).toBeNull();
    expect(screen.queryByRole('button', { name: 'Remove Board meeting' })).toBeNull();
    expect(screen.queryByRole('button', { name: 'Clear selection' })).toBeNull();
  });

  it('says when a file is in the trash, taken down or already held', async () => {
    api.searchAdminFiles.mockResolvedValue([
      media('33333333-3333-3333-3333-333333333333', 'Old call', 'old.mp4', {
        availability: 'trashed',
        hold: { held: true, held_by: ['organization'], hold_uuids: [] },
      }),
      media('44444444-4444-4444-4444-444444444444', 'Leaked clip', 'leak.mp4', {
        availability: 'quarantined',
      }),
    ]);
    render(FileSearchPicker, { props: { scope: 'platform' } } as never);
    await fireEvent.input(screen.getByRole('combobox'), { target: { value: 'o' } });
    expect(
      await screen.findByRole('option', { name: /Old call .*In the trash · Legal hold$/ })
    ).toBeInTheDocument();
    expect(screen.getByRole('option', { name: /Leaked clip .*Quarantined$/ })).toBeInTheDocument();
  });
});
