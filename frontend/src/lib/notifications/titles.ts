/**
 * Localised titles and client-composed messages for realtime notifications.
 *
 * Pure: the caller passes the current `Translate` function (`get(t)`), so the text is
 * resolved in the user's language at the moment the frame arrives.
 */
import type { Translate } from '$lib/i18n';
import type { NotificationData } from '$lib/notifications/types';

/**
 * Notification type -> i18n key. `download_progress` resurrects a key that shipped in all 12
 * locales but was dead (issue #569's currency review); `downloadNotifications.ts` is the first
 * caller.
 */
export const TITLE_KEYS: Readonly<Record<string, string>> = {
  transcription_status: 'notifications.transcriptionUpdate',
  summarization_status: 'notifications.summarizationUpdate',
  redaction_status: 'notifications.redactionUpdate',
  topic_extraction_status: 'notifications.topicExtraction',
  auto_label_status: 'notifications.autoLabeling',
  youtube_processing_status: 'notifications.youtubeProcessing',
  playlist_processing_status: 'notifications.playlistProcessing',
  analytics_status: 'notifications.analyticsUpdate',
  backup_status: 'notifications.backupUpdate',
  media_mirror_status: 'notifications.mediaMirrorUpdate',
  monitoring_alert: 'notifications.monitoringAlert',
  audio_extraction_status: 'notifications.audioExtraction',
  download_progress: 'notifications.downloadProgress',
  file_upload: 'notifications.fileUpload',
  file_created: 'notifications.fileCreated',
  file_updated: 'notifications.fileUpdated',
  file_deleted: 'notifications.fileDeleted',
  file_takedown: 'notifications.fileTakedown',
  file_takedown_released: 'notifications.fileTakedownReleased',
  collection_shared: 'notifications.collectionShared',
  collection_share_revoked: 'notifications.collectionShareRevoked',
  collection_share_updated: 'notifications.collectionShareUpdated',
  collection_access_changed: 'notifications.collectionAccessChanged',
  group_member_added: 'notifications.groupMemberAdded',
  group_member_removed: 'notifications.groupMemberRemoved',
  reindex_progress: 'notifications.searchReindexing',
  reindex_complete: 'notifications.searchReindexing',
  reindex_stopped: 'notifications.searchReindexing',
  migration_progress: 'notifications.embeddingMigration',
  migration_complete: 'notifications.embeddingMigration',
  migration_finalized: 'notifications.embeddingMigration',
  attribute_migration_progress: 'notifications.speakerAttributeMigration',
  attribute_migration_complete: 'notifications.speakerAttributeMigration',
  clustering_progress: 'notifications.speakerClustering',
  clustering_complete: 'notifications.speakerClustering',
  data_integrity_progress: 'notifications.dataIntegrity',
  data_integrity_complete: 'notifications.dataIntegrity',
  embedding_consistency_progress: 'notifications.embeddingConsistency',
  embedding_consistency_complete: 'notifications.embeddingConsistency',
};

/** The localised title for a notification `type`. */
export function notificationTitle(type: string, translate: Translate): string {
  const key = Object.hasOwn(TITLE_KEYS, type) ? TITLE_KEYS[type] : 'notifications.notification';
  return translate(key);
}

/**
 * The translated reason an AI task was refused by the LLM token limits (issue #790), or
 * `undefined` when the frame is not such a refusal. The backend's message is a fixed English
 * sentence; the `error_code` (and `budget_scope`) are what the client translates.
 */
export function llmBudgetMessage(
  data: Readonly<Record<string, unknown>>,
  translate: Translate
): string | undefined {
  if (data.error_code === 'llm_budget_unavailable') {
    return translate('notifications.llmBudgetUnavailable');
  }
  if (data.error_code !== 'llm_budget_exceeded') return undefined;
  return data.budget_scope === 'instance_month'
    ? translate('notifications.llmBudgetExceededMonth')
    : translate('notifications.llmBudgetExceededDay');
}

// Compose a localized message for types rendered client-side from payload
// fields (instead of the backend's English fallback message). Currently the
// DMCA takedown / release owner notices (§512(g)): the quarantined file 404s
// for its owner, so this notification is their only takedown surface and must
// carry the reason + counter-notice contact in the user's language.
export function composeClientMessage(
  type: string,
  data: NotificationData,
  translate: Translate
): string | undefined {
  const refused = llmBudgetMessage(data, translate);
  if (refused) return refused;
  if (type !== 'file_takedown' && type !== 'file_takedown_released') return undefined;
  const filename = data.filename || '';
  if (type === 'file_takedown_released') {
    return translate('notifications.fileTakedownReleasedMessage', { filename });
  }
  const fileId = data.file_uuid || '';
  const taken = translate('notifications.fileTakedownMessage', {
    filename,
    reason: data.reason || '',
  });
  const dispute = data.abuse_contact_email
    ? translate('notifications.fileTakedownCounterNotice', {
        email: data.abuse_contact_email,
        fileId,
      })
    : translate('notifications.fileTakedownCounterNoticeNoEmail', { fileId });
  return `${taken} ${dispute}`;
}
