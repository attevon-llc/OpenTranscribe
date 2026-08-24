import { writable, get } from 'svelte/store';
import { toastStore } from './toast';
import { t } from '$stores/locale';

export type DownloadType = 'video_with_subtitles' | 'original_video' | 'audio';

export interface DownloadState {
  fileId: string;
  filename: string;
  status: 'preparing' | 'processing' | 'downloading' | 'completed' | 'error';
  downloadType: DownloadType;
  progress?: number;
  startTime: Date;
  error?: string;
  notificationId?: string;
}

function createDownloadStore() {
  const { subscribe, set, update } = writable<Record<string, DownloadState>>({});

  return {
    subscribe,

    startDownload(
      fileId: string,
      filename: string,
      downloadType: DownloadType = 'video_with_subtitles'
    ): boolean {
      let canStart = false;

      update((downloads) => {
        // Check if file is already being downloaded
        const existing = downloads[fileId];
        if (existing && ['preparing', 'processing', 'downloading'].includes(existing.status)) {
          toastStore.warning(get(t)('downloads.alreadyProcessing', { filename }));
          return downloads;
        }

        canStart = true;

        // Create download state
        downloads[fileId] = {
          fileId,
          filename,
          status: 'preparing',
          downloadType,
          startTime: new Date(),
        };

        return downloads;
      });

      return canStart;
    },

    updateStatus(
      fileId: string,
      status: DownloadState['status'],
      progress?: number,
      error?: string
    ) {
      update((downloads) => {
        const download = downloads[fileId];
        if (!download) return downloads;

        download.status = status;
        if (progress !== undefined) download.progress = progress;
        if (error) download.error = error;

        switch (status) {
          case 'completed':
            // Remove from active downloads after a delay
            setTimeout(() => {
              this.removeDownload(fileId);
            }, 30000); // Keep for 30 seconds

            toastStore.success(
              get(t)('downloads.downloadedSuccessfully', {
                filename: download.filename,
              })
            );
            break;

          case 'error':
            toastStore.error(
              get(t)('downloads.downloadFailedError', {
                error: error || get(t)('downloads.unknownError'),
              })
            );

            // Remove from downloads after error
            setTimeout(() => {
              this.removeDownload(fileId);
            }, 60000); // Keep error for 1 minute
            break;
        }

        return downloads;
      });
    },

    removeDownload(fileId: string) {
      update((downloads) => {
        delete downloads[fileId];
        return downloads;
      });
    },

    isDownloading(fileId: string): boolean {
      // A pure read: must not go through `update()`, which calls `set()` on every
      // invocation and fires every subscriber regardless of whether anything
      // changed — reading this from a reactive context would create a feedback
      // loop. `get()` reads the current value without notifying anyone.
      const download = get({ subscribe })[fileId];
      return !!download && ['preparing', 'processing', 'downloading'].includes(download.status);
    },

    getDownloadStatus(fileId: string): DownloadState | null {
      return get({ subscribe })[fileId] || null;
    },

    /**
     * Full reset — clears all download state. Called on logout to prevent
     * User A's download list from leaking into User B's session.
     */
    reset() {
      set({});
    },
  };
}

export const downloadStore = createDownloadStore();
