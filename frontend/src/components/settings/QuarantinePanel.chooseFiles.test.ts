/**
 * Settings > Quarantine: the operator takes files down by searching for them and selecting
 * them (the gallery's takedown form with a picker), never by typing a file ID. Organization
 * scope offers no such action, matching the gallery, where only instance admins see Quarantine.
 */
import { describe, it, expect, vi, beforeEach } from 'vitest';
import { render, screen, fireEvent, waitFor } from '@testing-library/svelte';

vi.mock('$stores/locale', async () => {
  const mocks = await import('../../test-mocks/galleryToolbarMocks');
  const { readable } = await import('svelte/store');
  return { ...mocks.englishLocale(), locale: readable('en') };
});
vi.mock('$stores/toast', () => ({
  toastStore: { success: vi.fn(), error: vi.fn(), warning: vi.fn(), info: vi.fn() },
}));
const admin = vi.hoisted(() => ({
  listQuarantinedFiles: vi.fn(),
  quarantineFile: vi.fn(),
  releaseFile: vi.fn(),
}));
vi.mock('$lib/api/admin', () => ({ AdminApi: admin }));
const orgAdmin = vi.hoisted(() => ({ listQuarantinedFiles: vi.fn(), releaseFile: vi.fn() }));
vi.mock('$lib/api/orgAdmin', () => ({ OrgAdminApi: orgAdmin }));
const notices = vi.hoisted(() => ({ list: vi.fn(), create: vi.fn(), notify: vi.fn() }));
vi.mock('$lib/api/takedownNotices', () => ({ TakedownNoticesApi: notices }));
const fileSearch = vi.hoisted(() => ({ searchAdminFiles: vi.fn() }));
vi.mock('$lib/api/fileSearch', () => fileSearch);

import QuarantinePanel from './QuarantinePanel.svelte';

const CLIP = {
  uuid: 'f-1',
  title: 'Leaked clip',
  filename: 'clip.mp4',
  status: 'completed',
  upload_time: '2026-01-01T00:00:00Z',
};

beforeEach(() => {
  vi.clearAllMocks();
  admin.listQuarantinedFiles.mockResolvedValue({ files: [], total: 0 });
  orgAdmin.listQuarantinedFiles.mockResolvedValue({ files: [], total: 0 });
  notices.list.mockResolvedValue({ notices: [], total: 0 });
  fileSearch.searchAdminFiles.mockResolvedValue([CLIP]);
  admin.quarantineFile.mockResolvedValue({ uuid: 'f-1', is_quarantined: true });
});

describe('QuarantinePanel: Quarantine files by search and select', () => {
  it('takes down the file picked by name and refreshes the queue', async () => {
    render(QuarantinePanel, { scope: 'platform' });
    await waitFor(() => expect(admin.listQuarantinedFiles).toHaveBeenCalledTimes(1));

    await fireEvent.click(screen.getByRole('button', { name: 'Quarantine files' }));
    const search = await screen.findByRole('combobox', { name: 'Files to quarantine' });
    expect(screen.queryByText(/file id/i)).toBeNull();

    await fireEvent.input(search, { target: { value: 'leak' } });
    await fireEvent.click(await screen.findByRole('option', { name: /Leaked clip/ }));
    expect(fileSearch.searchAdminFiles).toHaveBeenCalledWith('platform', 'leak');

    await fireEvent.input(screen.getByPlaceholderText(/DMCA notice reference/i), {
      target: { value: 'Rights holder notice' },
    });
    const dialog = screen.getByRole('dialog');
    await fireEvent.click(dialog.querySelector('input[type="radio"][value="operator"]')!);
    await fireEvent.input(dialog.querySelector('#quarantine-operator-reason')!, {
      target: { value: 'routine review' },
    });
    await fireEvent.click(screen.getByRole('button', { name: 'Quarantine 1 file' }));

    await waitFor(() =>
      expect(admin.quarantineFile).toHaveBeenCalledWith('f-1', 'Rights holder notice', true, {
        operatorActionReason: 'routine review',
      })
    );
    await waitFor(() => expect(admin.listQuarantinedFiles).toHaveBeenCalledTimes(2));
  });

  it('keeps the submit disabled until a file is picked', async () => {
    render(QuarantinePanel, { scope: 'platform' });
    await fireEvent.click(await screen.findByRole('button', { name: 'Quarantine files' }));
    await fireEvent.input(await screen.findByPlaceholderText(/DMCA notice reference/i), {
      target: { value: 'Rights holder notice' },
    });
    expect(screen.getByRole('button', { name: 'Quarantine 0 files' })).toBeDisabled();
  });

  it('is offered to the operator, not in organization scope', async () => {
    const platform = render(QuarantinePanel, { scope: 'platform' });
    expect(await screen.findByRole('button', { name: 'Quarantine files' })).toBeInTheDocument();
    platform.unmount();

    render(QuarantinePanel, { scope: 'organization' });
    await waitFor(() => expect(orgAdmin.listQuarantinedFiles).toHaveBeenCalled());
    expect(screen.queryByRole('button', { name: 'Quarantine files' })).toBeNull();
  });
});
