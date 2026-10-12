/**
 * Media download SSE stream — extracted from `TranscriptDisplay.svelte` (issue #748 J5).
 *
 * The Export / Edit speakers / Download row (`TranscriptActionsBar`) moved into the
 * file-detail page's left column so it sits directly under the waveform, but the SSE
 * download stream it triggers is orchestration, not transcript-display logic — it belongs
 * with the other `$lib/fileDetail/*` modules (`notificationHandler.ts`, `segmentSync.ts`),
 * following the same "route page owns state and orchestration" pattern
 * (`components/fileDetail/CLAUDE.md`).
 *
 * `createDownloadStreamManager()` returns one manager per file-detail page mount. Call
 * `cleanup()` from the page's own `onDestroy` — it closes every open stream and clears every
 * pending watchdog timer, mirroring what `TranscriptDisplay`'s `onDestroy` used to do.
 */
import { get } from 'svelte/store';
import { downloadStore } from '$stores/downloads';
import { t } from '$stores/locale';
import axiosInstance from '$lib/axios';
import { getErrorMessage } from '$lib/utils/apiError';

/** Server-side download modes (mirror backend prepare-download). */
export type DownloadMode =
  | 'video_subtitles'
  | 'video_original'
  | 'audio_mp3'
  | 'audio_wav'
  | 'audio_original';

const DOWNLOAD_TYPE_BY_MODE: Record<
  DownloadMode,
  'video_with_subtitles' | 'original_video' | 'audio'
> = {
  video_subtitles: 'video_with_subtitles',
  video_original: 'original_video',
  audio_mp3: 'audio',
  audio_wav: 'audio',
  audio_original: 'audio',
};

const DOWNLOAD_STREAM_TIMEOUT_MS = 5 * 60 * 1000;

export interface DownloadStreamManager {
  /** Ask the server to prepare (or stream) a download and trigger it once ready. */
  downloadMedia: (
    file: { uuid?: string | number; filename?: string } | null | undefined,
    mode: DownloadMode
  ) => Promise<void>;
  /** Close every open stream and clear every pending watchdog. Call from `onDestroy`. */
  cleanup: () => void;
}

function triggerAnchorDownload(href: string, filename: string): void {
  const link = document.createElement('a');
  link.href = href;
  if (filename) link.download = filename;
  link.style.display = 'none';
  document.body.appendChild(link);
  link.click();
  document.body.removeChild(link);
}

export function createDownloadStreamManager(): DownloadStreamManager {
  // Always reads the CURRENT locale at call time (`get(t)`), not the locale active when
  // the manager was constructed — a page-lifetime singleton must not freeze translations.
  const translate = (key: string, options?: Record<string, unknown>) => get(t)(key, options);

  // Open SSE streams keyed by fileId so they can be cleaned up on completion/unmount.
  const downloadStreams = new Map<string, EventSource>();
  // Watchdog timers for the streams above, keyed the same way, so cleanup() can clear a
  // still-pending one rather than letting it fire after the page is gone.
  const downloadTimeouts = new Map<string, ReturnType<typeof setTimeout>>();

  function closeDownloadStream(fileId: string): void {
    const es = downloadStreams.get(fileId);
    if (es) {
      es.close();
      downloadStreams.delete(fileId);
    }
    const timeout = downloadTimeouts.get(fileId);
    if (timeout) {
      clearTimeout(timeout);
      downloadTimeouts.delete(fileId);
    }
  }

  // Subscribe to the server-pushed download stream. EventSource auto-reconnects on
  // transient drops, and the backend re-checks readiness on each connect, so the file is
  // still delivered even if the connection blips while ffmpeg runs.
  function openDownloadStream(fileId: string, mode: DownloadMode): void {
    closeDownloadStream(fileId);
    const es = new EventSource(`/api/files/${fileId}/download-stream?mode=${mode}`);
    downloadStreams.set(fileId, es);

    const timeout = setTimeout(() => {
      closeDownloadStream(fileId);
      downloadStore.updateStatus(
        fileId,
        'error',
        undefined,
        translate('transcript.downloadFailed')
      );
    }, DOWNLOAD_STREAM_TIMEOUT_MS);
    downloadTimeouts.set(fileId, timeout);

    es.addEventListener('progress', (e: MessageEvent) => {
      try {
        const d = JSON.parse(e.data);
        downloadStore.updateStatus(fileId, 'processing', d.progress);
      } catch {
        downloadStore.updateStatus(fileId, 'processing');
      }
    });

    es.addEventListener('ready', (e: MessageEvent) => {
      clearTimeout(timeout);
      closeDownloadStream(fileId);
      try {
        const d = JSON.parse(e.data);
        triggerAnchorDownload(d.url, d.filename ?? '');
        downloadStore.updateStatus(fileId, 'completed');
      } catch {
        downloadStore.updateStatus(
          fileId,
          'error',
          undefined,
          translate('transcript.downloadFailed')
        );
      }
    });

    es.addEventListener('error', (e: MessageEvent) => {
      // A server-sent `error` event carries a real failure (has data); a native transport
      // error has no data and EventSource will auto-reconnect.
      if (e?.data) {
        clearTimeout(timeout);
        closeDownloadStream(fileId);
        let msg = translate('transcript.downloadFailed');
        try {
          msg = JSON.parse(e.data).message || msg;
        } catch {
          /* keep the default message */
        }
        downloadStore.updateStatus(fileId, 'error', undefined, msg);
      }
    });
  }

  async function downloadMedia(
    file: { uuid?: string | number; filename?: string } | null | undefined,
    mode: DownloadMode
  ): Promise<void> {
    if (!file?.uuid) {
      throw new Error('downloadMedia called with no file');
    }

    const fileId = file.uuid.toString();
    const filename = file.filename ?? '';

    const canStart = downloadStore.startDownload(fileId, filename, DOWNLOAD_TYPE_BY_MODE[mode]);
    if (!canStart) return;

    try {
      downloadStore.updateStatus(fileId, 'processing');
      const { data } = await axiosInstance.post(`/files/${fileId}/prepare-download`, null, {
        params: { mode },
      });

      if (data.status === 'ready' && data.url) {
        // Browser streams straight from object storage — never buffered in memory.
        triggerAnchorDownload(data.url, data.filename ?? '');
        downloadStore.updateStatus(fileId, 'completed');
      } else {
        openDownloadStream(fileId, mode);
      }
    } catch (error: unknown) {
      downloadStore.updateStatus(
        fileId,
        'error',
        undefined,
        getErrorMessage(error, translate('transcript.downloadFailed'))
      );
    }
  }

  function cleanup(): void {
    downloadStreams.forEach((es) => es.close());
    downloadStreams.clear();
    downloadTimeouts.forEach((timeout) => clearTimeout(timeout));
    downloadTimeouts.clear();
  }

  return { downloadMedia, cleanup };
}
