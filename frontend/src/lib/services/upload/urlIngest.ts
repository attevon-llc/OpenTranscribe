import axiosInstance from '$lib/axios';
import axios, { type AxiosProgressEvent } from 'axios';
import { CONTROL_REQUEST_TIMEOUT_MS, type UploadHost, type UploadResult } from './types';

/** Hand a URL to the backend's `process-url` ingest; no body is uploaded from the browser. */
export async function processUrl(
  host: UploadHost,
  uploadId: string,
  url: string
): Promise<UploadResult> {
  const upload = host.getItem(uploadId)!;

  const cancelToken = axios.CancelToken.source();
  host.update(uploadId, {
    cancelToken,
    status: 'processing',
  });

  const response = await axiosInstance.post(
    '/files/process-url',
    {
      url: url.trim(),
      collection_ids: upload.collectionIds || undefined,
      tag_names: upload.tagNames || undefined,
      min_speakers: upload.minSpeakers ?? undefined,
      max_speakers: upload.maxSpeakers ?? undefined,
      num_speakers: upload.numSpeakers ?? undefined,
      whisper_model: upload.whisperModel || undefined,
      skip_summary: upload.skipSummary || undefined,
    },
    {
      timeout: CONTROL_REQUEST_TIMEOUT_MS,
      cancelToken: cancelToken.token,
      onUploadProgress: (progressEvent: AxiosProgressEvent) => {
        if (progressEvent.total) {
          const progress = Math.round((progressEvent.loaded * 100) / progressEvent.total);
          host.update(uploadId, { progress });
          host.emit('progress', uploadId, { progress });
        }
      },
    }
  );

  return { uuid: response.data.uuid, isDuplicate: false };
}
