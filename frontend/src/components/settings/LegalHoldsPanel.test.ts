/**
 * The legal-holds panel, for an organization's holds (#1276) and the operator's (legal-hold plan
 * §8.4). The server owns what a hold may name and what is still held after a release; the panel
 * must send exactly what the administrator entered to its own authority's API, keep "Place"
 * disabled until the input is one the server would accept, and surface the server's answer.
 */
import { describe, it, expect, vi, beforeEach } from 'vitest';
import { render, screen, fireEvent, waitFor } from '@testing-library/svelte';

vi.mock('$stores/locale', async () => {
  const { readable } = await import('svelte/store');
  const en = (await import('$lib/i18n/locales/en.json')).default as Record<string, string>;
  const t = (key: string, vars?: Record<string, unknown>) =>
    Object.entries(vars ?? {}).reduce(
      (text, [name, value]) => text.replace(`{{${name}}}`, String(value)),
      en[key] ?? en[`${key}_${vars?.count === 1 ? 'one' : 'other'}`] ?? key
    );
  return { t: readable(t), locale: readable('en') };
});

const api = vi.hoisted(() => ({
  listHolds: vi.fn(),
  getHold: vi.fn(),
  placeHold: vi.fn(),
  releaseHold: vi.fn(),
}));
vi.mock('$lib/api/orgAdmin', () => ({ OrgAdminApi: api }));
const adminApi = vi.hoisted(() => ({
  listHolds: vi.fn(),
  getHold: vi.fn(),
  placeHold: vi.fn(),
  releaseHold: vi.fn(),
}));
vi.mock('$lib/api/adminHolds', () => ({ AdminHoldsApi: adminApi }));
const collectionsApi = vi.hoisted(() => ({ list: vi.fn() }));
vi.mock('$lib/api/collections', () => ({ CollectionsApi: collectionsApi }));
const toast = vi.hoisted(() => ({ success: vi.fn(), error: vi.fn() }));
vi.mock('$stores/toast', () => ({ toastStore: toast }));
const fileSearch = vi.hoisted(() => ({ searchAdminFiles: vi.fn() }));
vi.mock('$lib/api/fileSearch', () => fileSearch);

import LegalHoldsPanel from './LegalHoldsPanel.svelte';

const COLLECTION = '22222222-2222-2222-2222-222222222222';
const FILE = '33333333-3333-3333-3333-333333333333';
const REASON = 'Litigation matter 14';

const hold = (over: Record<string, unknown> = {}) => ({
  uuid: 'h-1',
  scope: 'collection',
  collection_uuid: COLLECTION,
  collection_name: 'Matter 14',
  matter_ref: 'CASE-42',
  reason: 'Privileged litigation hold',
  placed_at: '2026-10-25T12:00:00Z',
  placed_by_display: 'Ada Admin',
  released_at: null,
  released_by_display: null,
  release_reason: null,
  active: true,
  file_count: 3,
  ...over,
});

beforeEach(() => {
  vi.clearAllMocks();
  api.listHolds.mockResolvedValue({ holds: [hold()], total: 1 });
  collectionsApi.list.mockResolvedValue([{ uuid: COLLECTION, name: 'Matter 14' }]);
});

async function openPlace() {
  render(LegalHoldsPanel);
  await screen.findByText('Collection: Matter 14');
  await fireEvent.click(screen.getByRole('button', { name: 'Place a hold' }));
  await screen.findByText('Place a legal hold');
}

/** The dialog opens on "Specific files"; these tests hold a whole collection. */
async function chooseCollection(): Promise<HTMLElement> {
  await fireEvent.click(screen.getByRole('radio', { name: 'A whole collection' }));
  return screen.findByRole('combobox', { name: 'Collection' });
}

describe('LegalHoldsPanel (organization scope, the default)', () => {
  it('lists holds in force with the reason, the matter, who placed it and the file count', async () => {
    render(LegalHoldsPanel);

    expect(await screen.findByText('Collection: Matter 14')).toBeInTheDocument();
    expect(screen.getByText('Privileged litigation hold')).toBeInTheDocument();
    expect(screen.getByText('CASE-42')).toBeInTheDocument();
    expect(screen.getByText('by Ada Admin')).toBeInTheDocument();
    expect(screen.getByText('In force')).toBeInTheDocument();
    expect(api.listHolds).toHaveBeenCalledWith({ active_only: true, limit: 200 });
  });

  it('shows the files a hold covers on request, by title, and hides them again', async () => {
    api.getHold.mockResolvedValue({
      ...hold(),
      files: [
        { uuid: FILE, title: 'Retail review', filename: 'r.mp3' },
        { uuid: COLLECTION, title: null, filename: 'untitled.mp3' },
      ],
    });
    render(LegalHoldsPanel);
    await screen.findByText('Collection: Matter 14');

    await fireEvent.click(screen.getByRole('button', { name: 'Show files' }));

    expect(await screen.findByText('Retail review')).toBeInTheDocument();
    expect(screen.getByText('untitled.mp3')).toBeInTheDocument();
    expect(api.getHold).toHaveBeenCalledWith('h-1');
    await fireEvent.click(screen.getByRole('button', { name: 'Hide files' }));
    expect(screen.queryByText('Retail review')).toBeNull();
  });

  it('says so when nothing is held', async () => {
    api.listHolds.mockResolvedValue({ holds: [], total: 0 });
    render(LegalHoldsPanel);
    expect(await screen.findByText('No legal holds in force.')).toBeInTheDocument();
  });

  it('asks for released holds too and offers no Release button on them', async () => {
    api.listHolds.mockResolvedValueOnce({ holds: [hold()], total: 1 });
    api.listHolds.mockResolvedValueOnce({
      holds: [hold(), hold({ uuid: 'h-2', active: false, released_at: '2026-10-26T12:00:00Z' })],
      total: 2,
    });
    render(LegalHoldsPanel);
    await screen.findByText('In force');

    await fireEvent.click(screen.getByLabelText('Show released holds'));

    await waitFor(() =>
      expect(api.listHolds).toHaveBeenLastCalledWith({ active_only: false, limit: 200 })
    );
    expect(await screen.findByText('Released')).toBeInTheDocument();
    expect(screen.getAllByRole('button', { name: 'Release' })).toHaveLength(1);
  });

  it('keeps Place disabled until a collection and a long enough reason are given, then sends them', async () => {
    api.placeHold.mockResolvedValue(hold());
    await openPlace();
    const place = screen.getByRole('button', { name: 'Place hold' });
    expect(place).toBeDisabled();

    await fireEvent.change(await chooseCollection(), { target: { value: COLLECTION } });
    expect(place).toBeDisabled(); // a collection alone is not enough
    const reasons = screen.getAllByRole('textbox');
    await fireEvent.input(reasons[0], { target: { value: 'too short' } });
    expect(place).toBeDisabled(); // 9 characters
    await fireEvent.input(reasons[0], { target: { value: `  ${REASON}  ` } });
    expect(place).toBeEnabled();
    await fireEvent.input(screen.getAllByRole('textbox')[1], { target: { value: ' CASE-42 ' } });

    await fireEvent.click(place);

    await waitFor(() =>
      expect(api.placeHold).toHaveBeenCalledWith({
        scope: 'collection',
        collection_uuid: COLLECTION,
        reason: REASON,
        matter_ref: 'CASE-42',
      })
    );
    expect(toast.success).toHaveBeenCalledWith('Legal hold placed on 3 files.');
    await waitFor(() => expect(api.listHolds).toHaveBeenCalledTimes(2));
  });

  it('holds named files picked by searching for them, never by typing an ID', async () => {
    api.placeHold.mockResolvedValue(hold({ scope: 'file', file_count: 1 }));
    fileSearch.searchAdminFiles.mockResolvedValue([
      { uuid: FILE, title: 'Deposition audio', filename: 'depo.wav' },
    ]);
    await openPlace();
    expect(screen.getByRole('radio', { name: 'Specific files' })).toBeChecked();
    expect(screen.queryByText(/file id/i)).toBeNull();

    await fireEvent.input(screen.getByTestId('place-hold-reason'), { target: { value: REASON } });
    expect(screen.getByRole('button', { name: 'Place hold' })).toBeDisabled(); // nothing picked

    await fireEvent.input(screen.getByRole('combobox', { name: 'Files to hold' }), {
      target: { value: 'depo' },
    });
    await fireEvent.click(await screen.findByRole('option', { name: /Deposition audio/ }));
    // The organization's own route: every member's file, not the admin's gallery.
    expect(fileSearch.searchAdminFiles).toHaveBeenCalledWith('organization', 'depo');
    expect(screen.getByRole('button', { name: 'Place hold' })).toBeEnabled();
    await fireEvent.click(screen.getByRole('button', { name: 'Place hold' }));

    await waitFor(() =>
      expect(api.placeHold).toHaveBeenCalledWith({
        scope: 'file',
        file_uuids: [FILE],
        reason: REASON,
        matter_ref: undefined,
      })
    );
  });

  it('shows the server refusal and does not close the dialog when placing fails', async () => {
    api.placeHold.mockRejectedValue({
      response: { status: 404, data: { detail: 'File not found' } },
    });
    await openPlace();
    await fireEvent.change(await chooseCollection(), { target: { value: COLLECTION } });
    await fireEvent.input(screen.getAllByRole('textbox')[0], { target: { value: REASON } });

    await fireEvent.click(screen.getByRole('button', { name: 'Place hold' }));

    await waitFor(() => expect(toast.error).toHaveBeenCalledWith('File not found'));
    expect(screen.getByText('Place a legal hold')).toBeInTheDocument();
  });

  it('releases only with a reason, then reloads', async () => {
    api.releaseHold.mockResolvedValue(hold({ active: false }));
    render(LegalHoldsPanel);
    await screen.findByText('Collection: Matter 14');

    await fireEvent.click(screen.getByRole('button', { name: 'Release' }));
    await screen.findByText('Release this legal hold?');
    const confirm = screen.getByRole('button', { name: 'Release hold' });
    expect(confirm).toBeDisabled();
    await fireEvent.input(screen.getByRole('textbox'), { target: { value: 'Matter closed 2026' } });
    expect(confirm).toBeEnabled();
    await fireEvent.click(confirm);

    await waitFor(() => expect(api.releaseHold).toHaveBeenCalledWith('h-1', 'Matter closed 2026'));
    expect(toast.success).toHaveBeenCalledWith('Legal hold released.');
    await waitFor(() => expect(api.listHolds).toHaveBeenCalledTimes(2));
  });
});

describe('LegalHoldsPanel (platform scope)', () => {
  beforeEach(() => {
    adminApi.listHolds.mockResolvedValue({
      holds: [hold({ authority: 'platform', organization_uuid: null })],
      total: 1,
    });
  });

  it('lists, places and releases through the operator API, never the organization one', async () => {
    adminApi.placeHold.mockResolvedValue(hold({ authority: 'platform', file_count: 3 }));
    adminApi.releaseHold.mockResolvedValue(hold({ active: false }));
    render(LegalHoldsPanel, { scope: 'platform' });
    await screen.findByText('Collection: Matter 14');
    expect(adminApi.listHolds).toHaveBeenCalledWith({ active_only: true, limit: 200 });

    await fireEvent.click(screen.getByRole('button', { name: 'Place a hold' }));
    await screen.findByText('Place a legal hold');
    await fireEvent.change(await chooseCollection(), { target: { value: COLLECTION } });
    await fireEvent.input(screen.getAllByRole('textbox')[0], { target: { value: REASON } });
    await fireEvent.click(screen.getByRole('button', { name: 'Place hold' }));
    await waitFor(() =>
      expect(adminApi.placeHold).toHaveBeenCalledWith({
        scope: 'collection',
        collection_uuid: COLLECTION,
        reason: REASON,
        matter_ref: undefined,
      })
    );

    await fireEvent.click(screen.getByRole('button', { name: 'Release' }));
    await fireEvent.input(screen.getByRole('textbox'), { target: { value: 'Matter closed 2026' } });
    await fireEvent.click(screen.getByRole('button', { name: 'Release hold' }));
    await waitFor(() =>
      expect(adminApi.releaseHold).toHaveBeenCalledWith('h-1', 'Matter closed 2026')
    );
    expect(api.listHolds).not.toHaveBeenCalled();
    expect(api.placeHold).not.toHaveBeenCalled();
    expect(api.releaseHold).not.toHaveBeenCalled();
  });

  it("says the operator's own words: instance administrators, and where takedown holds live", async () => {
    render(LegalHoldsPanel, { scope: 'platform' });
    await screen.findByText('Collection: Matter 14');
    expect(screen.getByText(/without taking them down/)).toBeInTheDocument();
    expect(
      screen.getByText('Holds placed together with a takedown are managed in Quarantine.')
    ).toBeInTheDocument();
    expect(screen.queryByText(/your organization's administrators only/)).toBeNull();
  });

  it('explains what a hold does at the top of the panel', async () => {
    render(LegalHoldsPanel, { scope: 'platform' });
    await fireEvent.click(
      await screen.findByRole('button', { name: 'What does a legal hold do?' })
    );
    expect(screen.getByText(/Still works:/)).toBeInTheDocument();
  });
});
