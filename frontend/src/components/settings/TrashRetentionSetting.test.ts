/**
 * The trash window field (issue #1101) in Settings, Data & Storage, File Retention: shows the
 * server's value, refuses anything outside 1-90 before the round trip, and saves ONLY
 * `trash_retention_days`, so it never re-sends the retention fields around it.
 */
import { beforeEach, describe, expect, it, vi } from 'vitest';
import { fireEvent, render, screen, waitFor } from '@testing-library/svelte';

vi.mock('$stores/locale', async () => {
  const { readable } = await import('svelte/store');
  const { englishLocale } = await import('$testMocks/galleryToolbarMocks');
  return { ...englishLocale(), locale: readable('en') };
});
const toast = vi.hoisted(() => ({ success: vi.fn(), error: vi.fn() }));
vi.mock('$stores/toast', () => ({ toastStore: toast }));
const axiosMock = vi.hoisted(() => ({ get: vi.fn(), put: vi.fn() }));
vi.mock('$lib/axios', () => ({ default: axiosMock }));

import TrashRetentionSetting from './TrashRetentionSetting.svelte';
import { clearSectionData, peekSectionData, SectionDataKey } from '$lib/settings/sectionData';

const config = (trash: number) => ({
  retention_enabled: false,
  retention_days: 365,
  run_time: '02:00',
  timezone: 'UTC',
  delete_error_files: false,
  last_run: null,
  last_run_deleted: 0,
  trash_retention_days: trash,
});

const input = () =>
  screen.getByLabelText('Days a deleted file stays in the trash') as HTMLInputElement;
const saveButton = () => screen.getByRole('button', { name: 'Save' }) as HTMLButtonElement;

beforeEach(() => {
  vi.clearAllMocks();
  clearSectionData();
});

describe('TrashRetentionSetting', () => {
  it('shows the server value and the help text about freeing storage', async () => {
    axiosMock.get.mockResolvedValue({ data: config(30) });
    render(TrashRetentionSetting);
    await waitFor(() => expect(input().value).toBe('30'));
    expect(screen.getByText(/storage is freed only when it is deleted forever/)).toBeTruthy();
    expect(saveButton().disabled).toBe(true);
  });

  it.each([0, 91, 2.5])('refuses %s before saving', async (value) => {
    axiosMock.get.mockResolvedValue({ data: config(30) });
    render(TrashRetentionSetting);
    await waitFor(() => expect(input().value).toBe('30'));
    await fireEvent.input(input(), { target: { value: String(value) } });
    expect(screen.getByRole('alert').textContent).toContain('from 1 to 90');
    expect(saveButton().disabled).toBe(true);
  });

  it('saves only the trash window and remembers what the server returned', async () => {
    axiosMock.get.mockResolvedValue({ data: config(30) });
    axiosMock.put.mockResolvedValue({ data: config(7) });
    render(TrashRetentionSetting);
    await waitFor(() => expect(input().value).toBe('30'));
    await fireEvent.input(input(), { target: { value: '7' } });
    await fireEvent.click(saveButton());
    await waitFor(() =>
      expect(axiosMock.put).toHaveBeenCalledWith('/admin/settings/retention-config', {
        trash_retention_days: 7,
      })
    );
    expect(toast.success).toHaveBeenCalledWith('Trash window saved.');
    expect(peekSectionData<{ trash_retention_days: number }>(SectionDataKey.retention)).toEqual(
      config(7)
    );
  });

  it('reports a refused save', async () => {
    axiosMock.get.mockResolvedValue({ data: config(30) });
    axiosMock.put.mockRejectedValue({ response: { status: 500, data: {} } });
    render(TrashRetentionSetting);
    await waitFor(() => expect(input().value).toBe('30'));
    await fireEvent.input(input(), { target: { value: '45' } });
    await fireEvent.click(saveButton());
    await waitFor(() =>
      expect(toast.error).toHaveBeenCalledWith('Could not save the trash window.')
    );
    expect(input().value).toBe('45');
  });
});
