/**
 * DEFECT THIS CATCHES (G3): `UploadStepExtraction` hardcoded the audio-size
 * estimate's bitrate to 32 kbps instead of reading the actually-configured
 * 64 kbps (`DEFAULT_EXTRACTION_CONFIG.bitrate`, the value real extraction
 * uses). The upload wizard therefore showed a size estimate half of what
 * extraction would actually produce — and half of what
 * `BulkAudioExtractionModal` (which hardcoded the correct 64) showed for the
 * identical file. This test renders the component and asserts its displayed
 * estimate matches the formula `BulkAudioExtractionModal` uses for the same
 * duration, so the two wizards never disagree again.
 */

import { describe, it, expect, vi } from 'vitest';
import { render } from '@testing-library/svelte';
import {
  estimateAudioSize,
  estimateDurationFromFileSize,
  formatFileSize,
} from '$lib/utils/metadataMapper';

vi.mock('$stores/locale', async () => {
  const { readable } = await import('svelte/store');
  const en = (await import('$lib/i18n/locales/en.json')).default as Record<string, string>;
  return { t: readable((key: string) => en[key] ?? key) };
});

import UploadStepExtraction from './UploadStepExtraction.svelte';

function videoFile(sizeBytes: number): File {
  const file = new File([new Uint8Array(1)], 'clip.mp4', { type: 'video/mp4' });
  Object.defineProperty(file, 'size', { value: sizeBytes });
  return file;
}

describe('UploadStepExtraction size estimate', () => {
  it("matches BulkAudioExtractionModal's estimate for the same file size (64 kbps)", () => {
    const file = videoFile(200 * 1024 * 1024); // 200MB

    // This is exactly the formula BulkAudioExtractionModal.svelte uses.
    const expectedDuration = estimateDurationFromFileSize(file.size);
    const expectedAudioSize = estimateAudioSize(expectedDuration, 64);

    const { container } = render(UploadStepExtraction, { props: { file, choice: 'extract' } });

    const audioValue = container.querySelector('.comparison-value.audio');
    expect(audioValue?.textContent).toBe(formatFileSize(expectedAudioSize));
  });

  it('control: shows the raw video size unchanged', () => {
    const file = videoFile(200 * 1024 * 1024);

    const { container } = render(UploadStepExtraction, { props: { file, choice: 'extract' } });

    const videoValue = container.querySelector('.comparison-value.video');
    expect(videoValue?.textContent).toBe(formatFileSize(file.size));
  });
});
