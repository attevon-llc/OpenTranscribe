/**
 * The /trash page (issue #1101): what each kind of caller sees, driven only by what
 * `GET /files/trash` returns. A member's rows come back with `can_purge: false` and their own
 * owner, an organization administrator's with other owners and `can_purge: true`; held and
 * past-due rows say why they are kept or going. Buttons appear only where the API would act.
 */
import { beforeEach, describe, expect, it, vi } from 'vitest';
import { fireEvent, render, screen, waitFor, within } from '@testing-library/svelte';
import type { TrashedFile, TrashPage } from '$lib/api/trash';

vi.mock('$stores/locale', async () => {
  const { readable } = await import('svelte/store');
  const { englishLocale } = await import('$testMocks/galleryToolbarMocks');
  return { ...englishLocale(), locale: readable('en') };
});
vi.mock('$stores/auth', async () => {
  const { readable } = await import('svelte/store');
  return { user: readable({ uuid: 'me' }) };
});
const toast = vi.hoisted(() => ({ success: vi.fn(), error: vi.fn() }));
vi.mock('$stores/toast', () => ({ toastStore: toast }));
const axiosMock = vi.hoisted(() => ({ get: vi.fn(), post: vi.fn(), delete: vi.fn() }));
vi.mock('$lib/axios', () => ({ default: axiosMock }));

import Page from './+page.svelte';

const DAY = 24 * 60 * 60 * 1000;
const iso = (offsetMs: number) => new Date(Date.now() + offsetMs).toISOString();

function row(overrides: Partial<TrashedFile> = {}): TrashedFile {
  return {
    uuid: 'f1',
    filename: 'meeting.wav',
    title: 'Weekly meeting',
    owner_uuid: 'me',
    owner_name: 'Me Myself',
    deleted_at: iso(-DAY),
    purge_after: iso(10 * DAY - 60_000),
    trash_origin: 'user',
    deleted_by_uuid: 'me',
    deleted_by_name: 'Me Myself',
    held: false,
    can_restore: true,
    can_purge: true,
    ...overrides,
  };
}

function pageOf(items: TrashedFile[], extra: Partial<TrashPage> = {}): { data: TrashPage } {
  return {
    data: { items, total: items.length, page: 1, page_size: 50, retention_days: 30, ...extra },
  };
}

const rows = () => screen.queryAllByTestId('trash-row');
const header = () =>
  Array.from(document.querySelectorAll('thead th')).map((th) => th.textContent?.trim());

beforeEach(() => {
  vi.clearAllMocks();
});

describe('/trash page states', () => {
  it('empty: says the trash is empty, offers nothing to empty, and states the window', async () => {
    axiosMock.get.mockResolvedValue(pageOf([], { retention_days: 14 }));
    render(Page);
    expect(await screen.findByText('The trash is empty')).toBeTruthy();
    expect(screen.getByTestId('trash-intro').textContent).toContain('for 14 days');
    expect(screen.queryByTestId('trash-empty')).toBeNull();
    expect(axiosMock.get).toHaveBeenCalledWith('/files/trash', {
      params: { page: 1, page_size: 50 },
    });
  });

  it('organization member: own files, Restore only, no owner column, no Empty trash', async () => {
    axiosMock.get.mockResolvedValue(pageOf([row({ can_purge: false })]));
    render(Page);
    await waitFor(() => expect(rows()).toHaveLength(1));
    const r = rows()[0];
    expect(within(r).getByTestId('trash-restore')).toBeTruthy();
    expect(within(r).queryByTestId('trash-purge')).toBeNull();
    expect(screen.queryByTestId('trash-empty')).toBeNull();
    expect(screen.getByTestId('trash-auto-purge-note')).toBeTruthy();
    expect(header()).not.toContain('Owner');
    expect(within(r).getByText('You')).toBeTruthy();
    expect(within(r).getByText('In 10 days')).toBeTruthy();
  });

  it('organization administrator: owner column, Delete forever and Empty trash', async () => {
    axiosMock.get.mockResolvedValue(
      pageOf([
        row(),
        row({
          uuid: 'f2',
          title: null,
          filename: 'interview.mp3',
          owner_uuid: 'm1',
          owner_name: 'Member One',
          deleted_by_uuid: 'm1',
          deleted_by_name: 'Member One',
        }),
      ])
    );
    render(Page);
    await waitFor(() => expect(rows()).toHaveLength(2));
    expect(header()).toContain('Owner');
    const theirs = rows()[1];
    expect(within(theirs).getByText('interview.mp3')).toBeTruthy();
    expect(within(theirs).getAllByText('Member One')).toHaveLength(2);
    expect(within(theirs).getByTestId('trash-purge')).toBeTruthy();
    expect(screen.getByTestId('trash-empty')).toBeTruthy();
    expect(screen.queryByTestId('trash-auto-purge-note')).toBeNull();
  });

  it('held row: the hold badge, "kept while on hold", restorable even past its date, never purgeable', async () => {
    axiosMock.get.mockResolvedValue(
      pageOf([row({ held: true, can_purge: false, purge_after: iso(-DAY) })])
    );
    render(Page);
    await waitFor(() => expect(rows()).toHaveLength(1));
    const r = rows()[0];
    expect(r.getAttribute('data-state')).toBe('held');
    expect(within(r).getByText('Legal hold')).toBeTruthy();
    expect(within(r).getByText('Kept while on hold')).toBeTruthy();
    expect(within(r).getByTestId('trash-restore')).toBeTruthy();
    expect(within(r).queryByTestId('trash-purge')).toBeNull();
  });

  it('past-due row: "being deleted now" and no actions, since the server would refuse them', async () => {
    axiosMock.get.mockResolvedValue(pageOf([row({ purge_after: iso(-60_000) })]));
    render(Page);
    await waitFor(() => expect(rows()).toHaveLength(1));
    const r = rows()[0];
    expect(r.getAttribute('data-state')).toBe('due');
    expect(within(r).getByText('Being deleted now')).toBeTruthy();
    expect(within(r).queryByTestId('trash-restore')).toBeNull();
    expect(within(r).queryByTestId('trash-purge')).toBeNull();
  });

  it('a failed load offers a retry that loads again', async () => {
    axiosMock.get.mockRejectedValueOnce(new Error('down')).mockResolvedValueOnce(pageOf([]));
    render(Page);
    await fireEvent.click(await screen.findByText('Retry'));
    expect(await screen.findByText('The trash is empty')).toBeTruthy();
  });
});

describe('/trash page actions', () => {
  it('Restore calls the restore route and reloads the list', async () => {
    axiosMock.get.mockResolvedValueOnce(pageOf([row()])).mockResolvedValueOnce(pageOf([]));
    axiosMock.post.mockResolvedValue({ data: {} });
    render(Page);
    await waitFor(() => expect(rows()).toHaveLength(1));
    await fireEvent.click(screen.getByTestId('trash-restore'));
    await waitFor(() => expect(axiosMock.post).toHaveBeenCalledWith('/files/f1/restore'));
    expect(toast.success).toHaveBeenCalledWith('Restored "Weekly meeting".');
    expect(await screen.findByText('The trash is empty')).toBeTruthy();
  });

  it('a NOT_RESTORABLE refusal is shown in the UI language, not the server English', async () => {
    axiosMock.get.mockResolvedValue(pageOf([row()]));
    axiosMock.post.mockRejectedValue({
      response: {
        status: 409,
        data: { detail: { error: 'NOT_RESTORABLE', message: 'server sentence' } },
      },
    });
    render(Page);
    await waitFor(() => expect(rows()).toHaveLength(1));
    await fireEvent.click(screen.getByTestId('trash-restore'));
    await waitFor(() =>
      expect(toast.error).toHaveBeenCalledWith(
        "This file's time in the trash has ended and it is being deleted."
      )
    );
  });

  it('Delete forever asks first, then calls the purge route', async () => {
    axiosMock.get.mockResolvedValue(pageOf([row()]));
    axiosMock.delete.mockResolvedValue({ data: { queued: 1 } });
    render(Page);
    await waitFor(() => expect(rows()).toHaveLength(1));
    await fireEvent.click(screen.getByTestId('trash-purge'));
    const dialog = await screen.findByRole('alertdialog');
    expect(within(dialog).getByText(/cannot be undone/)).toBeTruthy();
    expect(axiosMock.delete).not.toHaveBeenCalled();
    await fireEvent.click(within(dialog).getByRole('button', { name: 'Delete forever' }));
    await waitFor(() => expect(axiosMock.delete).toHaveBeenCalledWith('/files/f1/purge'));
    expect(toast.success).toHaveBeenCalledWith('The file is being deleted forever.');
  });

  it('Empty trash asks first, then calls the empty route', async () => {
    axiosMock.get.mockResolvedValue(pageOf([row()]));
    axiosMock.post.mockResolvedValue({ data: { queued: 1 } });
    render(Page);
    await waitFor(() => expect(rows()).toHaveLength(1));
    await fireEvent.click(screen.getByTestId('trash-empty'));
    const dialog = await screen.findByRole('alertdialog');
    expect(within(dialog).getByText(/Files on hold stay in the trash/)).toBeTruthy();
    await fireEvent.click(within(dialog).getByRole('button', { name: 'Empty trash' }));
    await waitFor(() => expect(axiosMock.post).toHaveBeenCalledWith('/files/trash/empty'));
  });

  it('reloads when a file is trashed or restored elsewhere (the realtime window events)', async () => {
    axiosMock.get.mockResolvedValue(pageOf([]));
    render(Page);
    await screen.findByText('The trash is empty');
    const before = axiosMock.get.mock.calls.length;
    window.dispatchEvent(new CustomEvent('file-trashed', { detail: { file_id: 'x' } }));
    window.dispatchEvent(new CustomEvent('file-restored', { detail: { file_id: 'x' } }));
    await waitFor(() => expect(axiosMock.get.mock.calls.length).toBe(before + 2));
  });
});
