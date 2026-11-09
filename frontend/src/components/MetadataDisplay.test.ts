/**
 * The models block that used to sit under "File details" is gone (#1229): it printed a
 * hardcoded English "Whisper:" label (even for cloud transcription) and rendered every
 * engine that was not `native` as "PyAnnote", so a pyannote.ai cloud run read as the local
 * engine. How a file was processed now lives in the "Processing" section
 * (`fileDetail/ProcessingSection.svelte`), rendered from the backend's `processing_steps`.
 */
import { describe, it, expect, vi } from 'vitest';
import { render, screen, fireEvent, waitFor } from '@testing-library/svelte';
import type { MediaFileDetail } from '$lib/types/media';

vi.mock('$stores/locale', async () =>
  (await import('$testMocks/localeMocks')).keyLocale({ code: 'en' })
);

vi.mock('$stores/auth', () => ({
  authStore: {
    subscribe: (run: (value: unknown) => void) => {
      run({ user: { email: 'admin@example.com' } });
      return () => {};
    },
  },
}));

vi.mock('../lib/axios', () => ({ default: { put: vi.fn() } }));

const toast = vi.hoisted(() => ({ success: vi.fn(), error: vi.fn() }));
vi.mock('$stores/toast', () => ({ toastStore: toast }));
const clipboard = vi.hoisted(() => ({ copied: [] as string[] }));
vi.mock('$lib/utils/clipboard', () => ({
  copyToClipboard: async (text: string, onSuccess?: () => void) => {
    clipboard.copied.push(text);
    onSuccess?.();
    return { success: true };
  },
}));

import MetadataDisplay from './MetadataDisplay.svelte';

function baseFile(overrides: Partial<MediaFileDetail> = {}): MediaFileDetail {
  return {
    uuid: 'file-1',
    filename: 'meeting.mp3',
    status: 'completed',
    ...overrides,
  } as MediaFileDetail;
}

describe('MetadataDisplay no longer carries the processing models (#1229)', () => {
  it('renders no hardcoded Whisper label and no engine guess for a cloud-diarized file', () => {
    const { container } = render(MetadataDisplay, {
      file: baseFile({
        whisper_model: 'large-v3-turbo',
        asr_provider: 'deepgram',
        asr_model: 'nova-3',
        diarization_provider: 'pyannote.ai',
        diarization_model: 'precision-3',
        embedding_mode: 'v4',
      }),
      showMetadata: true,
    });
    // The section did render, so the absences below are not an empty component.
    expect(screen.getByText('metadata.uploadDate:')).toBeTruthy();
    expect(container.textContent).not.toContain('Whisper:');
    expect(screen.queryByText('metadata.diarizationEngineLabel')).toBeNull();
    expect(screen.queryByText('metadata.diarizationEnginePyannote')).toBeNull();
    expect(container.textContent).not.toContain('precision-3');
    expect(container.textContent).not.toContain('nova-3');
  });
});

describe('MetadataDisplay: the file ID lives in the file details, not beside the title', () => {
  const UUID = '7a1b2c3d-0000-4000-8000-000000000001';

  it('shows the ID only once the details are opened, left to right, with a copy button', async () => {
    render(MetadataDisplay, { file: baseFile({ uuid: UUID }), showMetadata: false });
    expect(screen.queryByText(UUID)).toBeNull();

    await fireEvent.click(screen.getByRole('button', { name: 'metadata.fileDetails' }));
    expect(screen.getByText('metadata.fileId:')).toBeTruthy();
    expect(screen.getByText(UUID).getAttribute('dir')).toBe('ltr');

    await fireEvent.click(screen.getByRole('button', { name: 'fileDetail.copyId' }));
    expect(clipboard.copied).toEqual([UUID]);
    await waitFor(() => expect(toast.success).toHaveBeenCalledWith('fileDetail.copyIdDone'));
  });
});
