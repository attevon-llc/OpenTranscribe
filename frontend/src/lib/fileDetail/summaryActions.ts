/**
 * The file-detail page's summary requests: start (or force-regenerate) a summary and load
 * the stored one for the summary modal. Progress after a start arrives over the websocket.
 */
import { get } from 'svelte/store';
import axiosInstance from '$lib/axios';
import {
  requestSummary,
  topicExtractionNoticeKey,
  type SummarizeOptions,
} from '$lib/api/summarize';
import { getErrorStatus } from '$lib/utils/apiError';
import { t } from '$stores/locale';
import { toastStore } from '$stores/toast';

/**
 * Queue a summary, then say what happened to the tag/collection suggestions queued
 * alongside it, if anything. Rejects when the request fails; the caller owns that message.
 */
export async function startSummary(
  fileUuid: string,
  options: SummarizeOptions = {}
): Promise<void> {
  const res = await requestSummary(fileUuid, options);
  const key = topicExtractionNoticeKey(res.topic_extraction);
  if (key) toastStore.info(get(t)(key), 6000);
}

/**
 * The stored summary, or `undefined` when there is none. A 404 (no summary yet) is silent;
 * any other failure is toasted.
 */
export async function fetchSummaryData(fileUuid: string): Promise<unknown> {
  try {
    const response = await axiosInstance.get(`/files/${fileUuid}/summary`);
    return response.data.summary_data;
  } catch (error: unknown) {
    console.error('Error loading summary:', error);
    if (getErrorStatus(error) !== 404) {
      toastStore.error(get(t)('fileDetail.failedToLoadSummary'), 5000);
    }
    return undefined;
  }
}
