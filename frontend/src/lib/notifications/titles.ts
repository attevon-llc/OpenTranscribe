/**
 * Localised titles and client-composed messages for realtime notifications.
 *
 * Pure: the caller passes the current `Translate` function (`get(t)`), so the text is
 * resolved in the user's language at the moment the frame arrives.
 */
import type { Translate } from '$lib/i18n';
import type { NotificationData } from '$lib/notifications/types';

// Get a suitable title based on notification type
export function notificationTitle(type: string, translate: Translate): string {
  switch (type) {
    case 'transcription_status':
      return translate('notifications.transcriptionUpdate');
    case 'summarization_status':
      return translate('notifications.summarizationUpdate');
    case 'redaction_status':
      return translate('notifications.redactionUpdate');
    case 'topic_extraction_status':
      return translate('notifications.topicExtraction');
    case 'auto_label_status':
      return translate('notifications.autoLabeling');
    case 'youtube_processing_status':
      return translate('notifications.youtubeProcessing');
    case 'playlist_processing_status':
      return translate('notifications.playlistProcessing');
    case 'analytics_status':
      return translate('notifications.analyticsUpdate');
    case 'backup_status':
      return translate('notifications.backupUpdate');
    case 'media_mirror_status':
      return translate('notifications.mediaMirrorUpdate');
    case 'monitoring_alert':
      return translate('notifications.monitoringAlert');
    case 'audio_extraction_status':
      return translate('notifications.audioExtraction');
    case 'download_progress':
      // Resurrects a key that shipped in all 12 locales but was dead (issue
      // #569's currency review) — `downloadNotifications.ts` is the first caller.
      return translate('notifications.downloadProgress');
    case 'file_upload':
      return translate('notifications.fileUpload');
    case 'file_created':
      return translate('notifications.fileCreated');
    case 'file_updated':
      return translate('notifications.fileUpdated');
    case 'file_deleted':
      return translate('notifications.fileDeleted');
    case 'file_takedown':
      return translate('notifications.fileTakedown');
    case 'file_takedown_released':
      return translate('notifications.fileTakedownReleased');
    case 'collection_shared':
      return translate('notifications.collectionShared');
    case 'collection_share_revoked':
      return translate('notifications.collectionShareRevoked');
    case 'collection_share_updated':
      return translate('notifications.collectionShareUpdated');
    case 'collection_access_changed':
      return translate('notifications.collectionAccessChanged');
    case 'group_member_added':
      return translate('notifications.groupMemberAdded');
    case 'group_member_removed':
      return translate('notifications.groupMemberRemoved');
    case 'reindex_progress':
    case 'reindex_complete':
    case 'reindex_stopped':
      return translate('notifications.searchReindexing');
    case 'migration_progress':
    case 'migration_complete':
    case 'migration_finalized':
      return translate('notifications.embeddingMigration');
    case 'attribute_migration_progress':
    case 'attribute_migration_complete':
      return translate('notifications.speakerAttributeMigration');
    case 'clustering_progress':
    case 'clustering_complete':
      return translate('notifications.speakerClustering');
    case 'data_integrity_progress':
    case 'data_integrity_complete':
      return translate('notifications.dataIntegrity');
    case 'embedding_consistency_progress':
    case 'embedding_consistency_complete':
      return translate('notifications.embeddingConsistency');
    default:
      return translate('notifications.notification');
  }
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
