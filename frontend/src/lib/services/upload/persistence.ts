import type { UploadItem } from './types';

const STORAGE_KEY = 'upload_queue';

/** Write the queue to localStorage; persistence is optional, so failures are swallowed. */
export function persistUploads(uploads: Map<string, UploadItem>): void {
  try {
    const uploadsData = Array.from(uploads.entries()).map(([id, upload]) => [
      id,
      {
        ...upload,
        // Don't persist source data, cancel tokens, or a multipart session:
        // resuming one needs the File, which cannot survive a reload.
        source: upload.type === 'url' ? upload.source : null,
        cancelToken: undefined,
        multipart: undefined,
      },
    ]);
    localStorage.setItem(STORAGE_KEY, JSON.stringify(uploadsData));
  } catch {
    // localStorage persistence is optional
  }
}

/**
 * Read back the uploads worth retrying: queued or failed ones, reset to `queued`.
 * `queueIds` is the subset to re-enqueue (URL uploads only; files would need re-selection).
 */
export function loadPersistedUploads(): {
  entries: [string, UploadItem][];
  queueIds: string[];
} {
  const entries: [string, UploadItem][] = [];
  const queueIds: string[] = [];
  try {
    const stored = localStorage.getItem(STORAGE_KEY);
    if (stored) {
      const uploadsData = JSON.parse(stored);
      uploadsData.forEach(([id, upload]: [string, UploadItem]) => {
        if (upload.status === 'queued' || upload.status === 'failed') {
          upload.status = 'queued';
          upload.progress = 0;
          upload.error = undefined;
          entries.push([id, upload]);
          if (upload.type === 'url' && upload.source) {
            queueIds.push(id);
          }
        }
      });
    }
  } catch {
    // localStorage loading is optional
  }
  return { entries, queueIds };
}

export function discardPersistedUploads(): void {
  try {
    localStorage.removeItem(STORAGE_KEY);
  } catch {
    /* ignore */
  }
}
