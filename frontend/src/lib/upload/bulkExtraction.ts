import { get } from 'svelte/store';
import { t } from '$stores/locale';
import { toastStore } from '$stores/toast';
import { uploadsStore } from '$stores/uploads';
import type { UploadTranscriptionParams } from '$lib/services/uploadService';

/**
 * Load the FFmpeg.wasm wrapper on first use.
 *
 * A static import pulled `@ffmpeg/ffmpeg` + `@ffmpeg/util` into the home-page
 * chunk, so every visitor downloaded the wrapper even though client-side audio
 * extraction is an opt-in path taken only for video uploads. The dynamic
 * `import()` moves it to its own chunk, fetched the first time a user actually
 * extracts. The module caches itself, so repeat calls are free.
 */
export async function loadAudioExtractionService() {
  const { audioExtractionService } = await import('$lib/services/audioExtractionService');
  return audioExtractionService;
}

/** Extract audio from one video in the background and queue the result. */
export async function extractAndQueue(
  videoFile: File,
  transcriptionParams: UploadTranscriptionParams
): Promise<boolean> {
  try {
    const extractionService = await loadAudioExtractionService();
    const ea = await extractionService.extractAudio(videoFile);
    uploadsStore.addExtractedAudio(
      ea.blob,
      ea.filename,
      ea.metadata,
      ea.metadata.compressionRatio,
      transcriptionParams
    );
    toastStore.success(
      get(t)('uploader.audioExtractedSuccess', { ratio: ea.metadata.compressionRatio })
    );
    return true;
  } catch {
    toastStore.error(get(t)('uploader.failedToExtractAudio', { filename: videoFile.name }));
    return false;
  }
}

/** Extract audio from several videos concurrently; each result is queued as it finishes. */
export function startBulkExtraction(
  videoFiles: File[],
  transcriptionParams: UploadTranscriptionParams
) {
  toastStore.info(get(t)('uploader.extractingAudioFrom', { count: videoFiles.length }));
  videoFiles.forEach(async (videoFile) => {
    try {
      const ea = await (await loadAudioExtractionService()).extractAudio(videoFile);
      uploadsStore.addExtractedAudio(
        ea.blob,
        ea.filename,
        ea.metadata,
        ea.metadata.compressionRatio,
        transcriptionParams
      );
    } catch {
      toastStore.error(get(t)('uploader.failedToExtractAudio', { filename: videoFile.name }));
    }
  });
}
