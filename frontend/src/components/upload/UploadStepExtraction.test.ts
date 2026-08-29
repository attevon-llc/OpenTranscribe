/**
 * DEFECT THIS CATCHES (G3): `UploadStepExtraction` hardcoded the audio-size
 * estimate's bitrate to 32 kbps instead of reading the actually-configured
 * 64 kbps (`DEFAULT_EXTRACTION_CONFIG.bitrate`, the value real extraction
 * uses). The upload wizard therefore showed a size estimate half of what
 * extraction would actually produce — and half of what
 * `BulkAudioExtractionModal` (which ALSO hardcoded its own copy of 64, rather
 * than reading the config) showed for the identical file.
 *
 * FOLLOW-UP (adversarial-review, same G3 defect class): the original fix left
 * a "bug factory" in place — `BulkAudioExtractionModal` still hardcoded a
 * bare `64` (correct only by coincidence with `DEFAULT_EXTRACTION_CONFIG.bitrate`,
 * not derived from it) and `estimateAudioSize`'s own default parameter was
 * still the WRONG `32`. Either literal drifting from the real config
 * reproduces this exact bug. Both call sites now read
 * `DEFAULT_EXTRACTION_CONFIG.bitrate` directly and this test asserts against
 * that same config value rather than a copied literal, so a future change to
 * the configured bitrate cannot silently desync the two wizards again.
 */

import { describe, it, expect, vi } from 'vitest';
import { render } from '@testing-library/svelte';
import {
  estimateAudioSize,
  estimateDurationFromFileSize,
  formatFileSize,
} from '$lib/utils/metadataMapper';
import { DEFAULT_EXTRACTION_CONFIG } from '$lib/types/audioExtraction';

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
  it("matches BulkAudioExtractionModal's estimate for the same file size (DEFAULT_EXTRACTION_CONFIG.bitrate)", () => {
    const file = videoFile(200 * 1024 * 1024); // 200MB

    // This is exactly the formula BulkAudioExtractionModal.svelte uses — both
    // read the bitrate from the shared config, never a copied literal.
    const expectedDuration = estimateDurationFromFileSize(file.size);
    const expectedAudioSize = estimateAudioSize(
      expectedDuration,
      DEFAULT_EXTRACTION_CONFIG.bitrate
    );

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
