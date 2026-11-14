/**
 * Labels for `Task.task_type`, the value the System statistics recent-tasks table shows.
 *
 * Source of truth is the backend: every `create_task_record(..., "<type>")` call under
 * backend/app/tasks (plus the `TASK_TYPE` constant of privacy_purge_task.py and the
 * ProgressTracker(task_type=...) callers). `taskTypeLabels.test.ts` reads those files from
 * disk and fails when a type has no entry here.
 */
type Translate = (key: string) => string;

export const TASK_TYPE_LABEL_KEYS: Record<string, string> = {
  transcription: 'notifications.job.task.transcription',
  redaction_detection: 'notifications.job.task.redaction',
  summarization: 'notifications.job.task.summary',
  topic_extraction: 'notifications.job.task.topics',
  analytics: 'settings.statistics.taskType.analytics',
  attribute_migration: 'settings.statistics.taskType.attributeMigration',
  clustering: 'settings.statistics.taskType.speakerClustering',
  speaker_clustering: 'settings.statistics.taskType.speakerClustering',
  combined_speaker_migration: 'settings.statistics.taskType.combinedSpeakerMigration',
  embedding_consistency: 'settings.statistics.taskType.embeddingConsistency',
  migration: 'settings.statistics.taskType.migration',
  privacy_purge: 'settings.statistics.taskType.privacyPurge',
  rediarize: 'settings.statistics.taskType.rediarize',
  reindex: 'settings.statistics.taskType.reindex',
  search_indexing: 'settings.statistics.taskType.searchIndexing',
  search_reembed: 'settings.statistics.taskType.searchReembed',
  speaker_attribute_detection: 'settings.statistics.taskType.speakerAttributeDetection',
  speaker_embedding: 'settings.statistics.taskType.speakerEmbedding',
  speaker_identification: 'settings.statistics.taskType.speakerIdentification',
};

/** Translated label for a task type; an unknown type falls back to readable Title Case. */
export function taskTypeLabel(type: string, translate: Translate): string {
  const key = TASK_TYPE_LABEL_KEYS[type];
  if (key) return translate(key);
  return type.replace(/_/g, ' ').replace(/\b\w/g, (c) => c.toUpperCase());
}
