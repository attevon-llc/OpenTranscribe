import { writable, get } from 'svelte/store';
import { addNotification } from './notifications';
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

// i18n key fragments per download type, so notifications match what is actually
// being downloaded (e.g. audio downloads don't claim to be "adding subtitles").
const DOWNLOAD_COPY: Record<
  DownloadType,
  { started: string; preparing: string; processing: string }
> = {
  video_with_subtitles: {
    started: 'downloads.videoDownloadStarted',
    preparing: 'downloads.preparingWithSubtitles',
    processing: 'downloads.addingSubtitles',
  },
  original_video: {
    started: 'downloads.downloadStarted',
    preparing: 'downloads.preparingDownload',
    processing: 'downloads.preparingDownload',
  },
  audio: {
    started: 'downloads.audioDownloadStarted',
    preparing: 'downloads.preparingAudio',
    processing: 'downloads.extractingAudio',
  },
};

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

        const copy = DOWNLOAD_COPY[downloadType];

        // Add persistent notification
        addNotification({
          title: get(t)(copy.started),
          message: get(t)(copy.preparing, { filename }),
          type: 'info',
          read: false,
          data: { file_id: fileId, download_type: downloadType },
        });

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

        const copy = DOWNLOAD_COPY[download.downloadType];

        // Update notification based on status
        switch (status) {
          case 'processing':
            addNotification({
              title: get(t)('downloads.processingVideo'),
              message: get(t)(copy.processing, {
                filename: download.filename,
              }),
              type: 'info',
              read: false,
              data: { file_id: fileId, download_type: 'processing' },
            });
            break;

          case 'downloading':
            addNotification({
              title: get(t)('downloads.processingVideo'),
              message: get(t)(copy.processing, {
                filename: download.filename,
              }),
              type: 'info',
              read: false,
              data: { file_id: fileId, download_type: 'ready' },
            });
            break;

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
            addNotification({
              title: get(t)('downloads.downloadFailed'),
              message: get(t)('downloads.failedToProcess', {
                filename: download.filename,
                error: error || get(t)('downloads.unknownError'),
              }),
              type: 'error',
              read: false,
              data: { file_id: fileId, download_type: 'video_error' },
            });

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
      let result = false;
      update((downloads) => {
        const download = downloads[fileId];
        result = download && ['preparing', 'processing', 'downloading'].includes(download.status);
        return downloads;
      });
      return result;
    },

    getDownloadStatus(fileId: string): DownloadState | null {
      let result: DownloadState | null = null;
      update((downloads) => {
        result = downloads[fileId] || null;
        return downloads;
      });
      return result;
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
