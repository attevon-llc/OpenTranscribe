/**
 * The file page header under a legal hold (legal-hold plan, Lane D): the title area shows the
 * file and nothing about holds or IDs (no Copy file ID, no Place legal hold, no hold pill beside
 * the title, owner feedback 2026-11-08); the held-file banner below it says the file is held,
 * the title edit is disabled with its reason, and a title save refused by a hold placed after
 * the page loaded shows the translated refusal instead of the generic "could not save" dialog.
 */
import { describe, it, expect, vi, beforeEach } from 'vitest';
import { render, screen, fireEvent, waitFor } from '@testing-library/svelte';
import type { MediaFileDetail } from '$lib/types/media';
import type { HoldSummary } from '$lib/types/hold';

const mockAxios = vi.hoisted(() => ({ put: vi.fn() }));
vi.mock('$lib/axios', () => ({ default: mockAxios, isRequestCancelled: () => false }));
vi.mock('$stores/locale', async () => {
  const mocks = await import('../../test-mocks/galleryToolbarMocks');
  const { readable } = await import('svelte/store');
  return { ...mocks.englishLocale(), locale: readable('en') };
});
const toast = vi.hoisted(() => ({ success: vi.fn(), error: vi.fn(), info: vi.fn() }));
vi.mock('$stores/toast', () => ({ toastStore: toast }));
vi.mock('$stores/auth', async () => {
  const { readable } = await import('svelte/store');
  return { user: readable({ uuid: 'u-admin', role: 'admin' }) };
});

import FrozenContextTestHost from './FrozenContextTestHost.svelte';
import FileHeader from '$components/FileHeader.svelte';

const FILE_UUID = '7a1b2c3d-0000-4000-8000-000000000001';

function file(over: Partial<MediaFileDetail> = {}): MediaFileDetail {
  return {
    uuid: FILE_UUID,
    filename: 'meeting.mp4',
    title: 'Board meeting',
    status: 'completed',
    upload_time: '2026-01-01T00:00:00Z',
    ...over,
  } as MediaFileDetail;
}

const HELD: HoldSummary = { held: true, held_by: ['organization'], hold_uuids: [] };

function header(f: MediaFileDetail, frozen: boolean) {
  return render(FrozenContextTestHost, {
    props: { component: FileHeader, props: { file: f }, frozen },
  } as never);
}

beforeEach(() => {
  vi.clearAllMocks();
});

describe('FileHeader: the title area shows the file, not hold or ID controls', () => {
  it('has no Copy file ID and no Place legal hold, even for an administrator', () => {
    header(file(), false);
    expect(screen.queryByRole('button', { name: /copy file id/i })).toBeNull();
    expect(screen.queryByRole('button', { name: /legal hold/i })).toBeNull();
    expect(screen.queryByText(FILE_UUID)).toBeNull();
  });

  it('a held file says so in the banner below the title, not in the title row', () => {
    header(file({ hold: HELD }), true);
    const title = screen.getByRole('heading', { level: 1, name: 'Board meeting' });
    const titleRow = title.parentElement as HTMLElement;
    expect(titleRow).not.toHaveTextContent(/legal hold/i);
    expect(titleRow.contains(screen.getByTestId('held-file-banner'))).toBe(false);
  });
});

describe('FileHeader: held file', () => {
  it('shows the banner and disables the title edit, pointing at the banner', () => {
    header(file({ hold: HELD }), true);
    expect(screen.getByTestId('held-file-banner')).toHaveTextContent(
      'An administrator of your organization placed it.'
    );
    const edit = screen.getByRole('button', { name: 'Edit display name' });
    expect(edit).toBeDisabled();
    expect(
      document.getElementById(edit.getAttribute('aria-describedby') as string)
    ).toHaveTextContent(/cannot be changed or deleted until the hold is released/);
  });

  it('has no banner and an enabled title edit when the file is free', () => {
    header(file(), false);
    expect(screen.queryByTestId('held-file-banner')).toBeNull();
    expect(screen.getByRole('button', { name: 'Edit display name' })).toBeEnabled();
  });

  it('a title save refused by a hold placed meanwhile shows the hold sentence, not the save error', async () => {
    mockAxios.put.mockRejectedValue({
      response: {
        status: 409,
        data: {
          detail: { error: 'FILE_UNDER_LEGAL_HOLD', message: 'refused', held_by: ['platform'] },
        },
      },
    });
    header(file(), false);
    await fireEvent.click(screen.getByRole('button', { name: 'Edit display name' }));
    const input = screen.getByPlaceholderText('Enter display name');
    await fireEvent.input(input, { target: { value: 'New title' } });
    await fireEvent.keyDown(input, { key: 'Enter' });

    await waitFor(() =>
      expect(toast.error).toHaveBeenCalledWith(
        expect.stringContaining('legal hold placed by the service operator')
      )
    );
    expect(screen.queryByPlaceholderText('Enter display name')).toBeNull();
  });
});
