import { describe, it, expect, vi, beforeEach } from 'vitest';
import { render, fireEvent, waitFor } from '@testing-library/svelte';

vi.mock('$stores/locale', async () => {
  const { readable } = await import('svelte/store');
  const en = (await import('$lib/i18n/locales/en.json')).default as Record<string, string>;
  return {
    t: readable((key: string, opts?: Record<string, unknown>) => {
      const count = opts?.count;
      const plural =
        typeof count === 'number' ? (count === 1 ? `${key}_one` : `${key}_other`) : undefined;
      let out = (plural && en[plural]) ?? en[key] ?? key;
      for (const [name, value] of Object.entries(opts ?? {})) {
        out = out.split(`{{${name}}}`).join(String(value));
      }
      return out;
    }),
  };
});

const api = vi.hoisted(() => ({
  AdminApi: {
    quarantineFile: vi.fn(),
  },
}));

vi.mock('$lib/api/admin', () => api);

vi.mock('$stores/toast', () => ({
  toastStore: { success: vi.fn(), error: vi.fn(), warning: vi.fn(), info: vi.fn() },
}));

import QuarantineModal from './QuarantineModal.svelte';
import type { MediaFile } from '$lib/types/media';

function makeFile(overrides: Partial<MediaFile> = {}): MediaFile {
  return {
    uuid: 'f-1',
    filename: 'clip.mp4',
    status: 'completed',
    upload_time: '2026-01-01T00:00:00Z',
    ...overrides,
  } as MediaFile;
}

describe('QuarantineModal', () => {
  beforeEach(() => {
    vi.clearAllMocks();
  });

  it('disables submit with an empty reason', () => {
    const { getByText } = render(QuarantineModal, {
      props: { isOpen: true, files: [makeFile()] },
    });
    const submitBtn = getByText(/gallery\.quarantine\.submit|Quarantine 1 file/i).closest(
      'button'
    ) as HTMLButtonElement;
    expect(submitBtn.disabled).toBe(true);
  });

  it('disables submit with a whitespace-only reason', async () => {
    const { getByPlaceholderText, getByText } = render(QuarantineModal, {
      props: { isOpen: true, files: [makeFile()] },
    });
    const textarea = getByPlaceholderText(
      /gallery\.quarantine\.reasonPlaceholder|DMCA notice reference/i
    ) as HTMLTextAreaElement;
    await fireEvent.input(textarea, { target: { value: '   ' } });

    const submitBtn = getByText(/gallery\.quarantine\.submit|Quarantine 1 file/i).closest(
      'button'
    ) as HTMLButtonElement;
    expect(submitBtn.disabled).toBe(true);
  });

  it('enables submit once a real reason is typed, and posts reason + legal_hold', async () => {
    api.AdminApi.quarantineFile.mockResolvedValue({
      uuid: 'f-1',
      is_quarantined: true,
      legal_hold: true,
      status: 'quarantined',
    });
    const { getByPlaceholderText, getByText } = render(QuarantineModal, {
      props: { isOpen: true, files: [makeFile()] },
    });
    const textarea = getByPlaceholderText(
      /gallery\.quarantine\.reasonPlaceholder|DMCA notice reference/i
    ) as HTMLTextAreaElement;
    await fireEvent.input(textarea, { target: { value: 'DMCA notice #42' } });

    const submitBtn = getByText(/gallery\.quarantine\.submit|Quarantine 1 file/i).closest(
      'button'
    ) as HTMLButtonElement;
    expect(submitBtn.disabled).toBe(false);

    await fireEvent.click(submitBtn);

    await waitFor(() =>
      expect(api.AdminApi.quarantineFile).toHaveBeenCalledWith('f-1', 'DMCA notice #42', true)
    );
  });

  it('the legal-hold checkbox is checked on mount', () => {
    const { container } = render(QuarantineModal, {
      props: { isOpen: true, files: [makeFile()] },
    });
    const checkbox = container.querySelector('input[type="checkbox"]') as HTMLInputElement;
    expect(checkbox.checked).toBe(true);
  });

  it('the char counter reflects the 2000-character limit', () => {
    const { getByText } = render(QuarantineModal, {
      props: { isOpen: true, files: [makeFile()] },
    });
    expect(getByText('0 / 2000')).toBeTruthy();
  });

  it('Cancel calls no API', async () => {
    const { getByText } = render(QuarantineModal, {
      props: { isOpen: true, files: [makeFile()] },
    });

    await fireEvent.click(getByText('Cancel'));

    expect(api.AdminApi.quarantineFile).not.toHaveBeenCalled();
  });
});
