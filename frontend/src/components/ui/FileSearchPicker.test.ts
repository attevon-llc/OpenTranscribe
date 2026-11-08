/**
 * The administrator's file picker (legal hold, quarantine): type a title or file name, pick
 * from what the gallery's own list endpoint returns, see the picks as removable chips. Nobody
 * types a file ID.
 */
import { describe, it, expect, vi, beforeEach } from 'vitest';
import { render, screen, fireEvent } from '@testing-library/svelte';
import type { MediaFile } from '$lib/types/media';

vi.mock('$stores/locale', async () =>
  (await import('../../test-mocks/galleryToolbarMocks')).englishLocale()
);
const api = vi.hoisted(() => ({ searchFiles: vi.fn() }));
vi.mock('$lib/api/fileSearch', () => api);

import FileSearchPicker from './FileSearchPicker.svelte';

function media(uuid: string, title: string, filename = `${title}.mp4`): MediaFile {
  return {
    uuid,
    title,
    filename,
    status: 'completed',
    upload_time: '2026-01-01T00:00:00Z',
    formatted_upload_date: 'Jan 1, 2026',
  } as MediaFile;
}

const BOARD = media('11111111-1111-1111-1111-111111111111', 'Board meeting', 'board.mp4');
const BUDGET = media('22222222-2222-2222-2222-222222222222', 'Budget review', 'budget.mp4');

beforeEach(() => {
  vi.clearAllMocks();
  api.searchFiles.mockResolvedValue([BOARD, BUDGET]);
});

async function typeAndPick(query: string, option: RegExp) {
  await fireEvent.input(screen.getByRole('combobox'), { target: { value: query } });
  await fireEvent.click(await screen.findByRole('option', { name: option }));
}

describe('FileSearchPicker', () => {
  it('searches by what was typed and adds the picked file as a chip, never asking for an ID', async () => {
    const changed = vi.fn();
    render(FileSearchPicker, {
      props: { label: 'Files to hold' },
      events: { change: (e: CustomEvent<MediaFile[]>) => changed(e.detail) },
    } as never);

    // The field is labelled and searches by title or file name.
    const input = screen.getByRole('combobox', { name: 'Files to hold' });
    expect(input).toHaveAttribute('placeholder', 'Search by title or file name');

    await typeAndPick('board', /Board meeting · board\.mp4 · Jan 1, 2026/);

    expect(api.searchFiles).toHaveBeenCalledWith('board');
    expect(changed).toHaveBeenLastCalledWith([BOARD]);
    expect(screen.getByTestId('file-search-picker-chips')).toHaveTextContent('Board meeting');
    expect(screen.getByText('Selected: 1')).toBeInTheDocument();
    expect(screen.queryByText(/file id/i)).toBeNull();
  });

  it('does not offer a file that is already selected', async () => {
    render(FileSearchPicker, { props: { selected: [BOARD] } } as never);
    await fireEvent.input(screen.getByRole('combobox'), { target: { value: 'b' } });
    expect(await screen.findByRole('option', { name: /Budget review/ })).toBeInTheDocument();
    expect(screen.queryByRole('option', { name: /Board meeting/ })).toBeNull();
  });

  it('removes one chip, or clears them all', async () => {
    const changed = vi.fn();
    render(FileSearchPicker, {
      props: { selected: [BOARD, BUDGET] },
      events: { change: (e: CustomEvent<MediaFile[]>) => changed(e.detail) },
    } as never);

    await fireEvent.click(screen.getByRole('button', { name: 'Remove Board meeting' }));
    expect(changed).toHaveBeenLastCalledWith([BUDGET]);

    await fireEvent.click(screen.getByRole('button', { name: 'Clear selection' }));
    expect(changed).toHaveBeenLastCalledWith([]);
    expect(screen.queryByTestId('file-search-picker-chips')).toBeNull();
  });

  it('replaces the search with a note once the limit is reached', () => {
    render(FileSearchPicker, { props: { selected: [BOARD, BUDGET], max: 2 } } as never);
    expect(screen.queryByRole('combobox')).toBeNull();
    expect(screen.getByRole('status')).toHaveTextContent('The limit of 2 files is reached.');
  });

  it('while the parent submits, shows the chips but allows no change', () => {
    render(FileSearchPicker, { props: { selected: [BOARD], disabled: true } } as never);
    expect(screen.getByTestId('file-search-picker-chips')).toHaveTextContent('Board meeting');
    expect(screen.queryByRole('combobox')).toBeNull();
    expect(screen.queryByRole('button', { name: 'Remove Board meeting' })).toBeNull();
    expect(screen.queryByRole('button', { name: 'Clear selection' })).toBeNull();
  });
});
