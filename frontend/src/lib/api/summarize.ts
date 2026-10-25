/**
 * Ask the server for a file's AI summary (`POST /files/{uuid}/summarize`).
 *
 * The response also says what happened to topic extraction (AI tags and collections), which
 * the server queues alongside a summary for a file that never had it
 * (docs/design/llm_added_regenerate_plan.md). The server decides; this only names the message.
 */
import axiosInstance from '$lib/axios';

/** Wire values of `topic_extraction` (backend `services/ai_insights_companion.TopicCompanion`). */
export type TopicExtractionOutcome =
  | 'queued'
  | 'already_extracted'
  | 'in_progress'
  | 'disabled_by_setting'
  | 'unavailable';

export interface SummarizeResponse {
  message: string;
  task_id: string;
  file_id: string;
  topic_extraction?: TopicExtractionOutcome;
}

export interface SummarizeOptions {
  forceRegenerate?: boolean;
  /** A saved prompt; null/absent uses the default prompt. */
  promptUuid?: string | null;
}

export async function requestSummary(
  fileUuid: string,
  options: SummarizeOptions = {}
): Promise<SummarizeResponse> {
  const body: Record<string, unknown> = {};
  if (options.forceRegenerate) body.force_regenerate = true;
  if (options.promptUuid) body.prompt_uuid = options.promptUuid;
  const response = await axiosInstance.post<SummarizeResponse>(
    `/files/${fileUuid}/summarize`,
    Object.keys(body).length ? body : undefined
  );
  return response.data;
}

/**
 * The one sentence worth telling the user about topic extraction, or null. Only two outcomes
 * change what they will see: tags are on their way, or they will not come because the
 * automatic setting is off. The rest (already there, already running, not possible) need no
 * words.
 */
export function topicExtractionNoticeKey(
  outcome: TopicExtractionOutcome | undefined
): string | null {
  switch (outcome) {
    case 'queued':
      return 'fileDetail.topicSuggestionsQueued';
    case 'disabled_by_setting':
      return 'fileDetail.topicSuggestionsSkippedBySetting';
    default:
      return null;
  }
}
