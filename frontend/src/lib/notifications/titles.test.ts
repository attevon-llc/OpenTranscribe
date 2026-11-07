/**
 * Pins the realtime notification text: which i18n key titles each frame type, and the
 * client-composed DMCA takedown notice. A wrong key renders as a raw key path in every
 * locale; a dropped counter-notice contact removes the owner's only dispute surface.
 */
import { describe, it, expect } from 'vitest';
import type { Translate } from '$lib/i18n';
import { composeClientMessage, notificationTitle } from './titles';

const echo: Translate = (key, opts) => (opts ? `${key}:${JSON.stringify(opts)}` : key);

const EXPECTED_TITLE_KEYS: Record<string, string> = {
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

describe('notificationTitle', () => {
  it.each(Object.entries(EXPECTED_TITLE_KEYS))('%s -> %s', (type, key) => {
    expect(notificationTitle(type, echo)).toBe(key);
  });

  it('falls back to the generic title for an unknown type', () => {
    expect(notificationTitle('post_processing_status', echo)).toBe('notifications.notification');
    expect(notificationTitle('no_such_type', echo)).toBe('notifications.notification');
  });
});

describe('composeClientMessage', () => {
  it('returns undefined for every non-takedown type, so the backend message is used', () => {
    expect(composeClientMessage('transcription_status', { filename: 'a' }, echo)).toBeUndefined();
  });

  it('names the file and reason and the counter-notice contact on a takedown', () => {
    const msg = composeClientMessage(
      'file_takedown',
      {
        filename: 'talk.mp3',
        reason: 'dmca',
        abuse_contact_email: 'abuse@example.com',
        file_uuid: 'f-1',
      },
      echo
    );
    expect(msg).toBe(
      'notifications.fileTakedownMessage:{"filename":"talk.mp3","reason":"dmca"} ' +
        'notifications.fileTakedownCounterNotice:{"email":"abuse@example.com","fileId":"f-1"}'
    );
  });

  it('uses the no-email counter-notice copy when no contact is configured', () => {
    const msg = composeClientMessage('file_takedown', {}, echo);
    expect(msg).toBe(
      'notifications.fileTakedownMessage:{"filename":"","reason":""} ' +
        'notifications.fileTakedownCounterNoticeNoEmail:{"fileId":""}'
    );
  });

  it('composes the release notice from the filename alone', () => {
    expect(composeClientMessage('file_takedown_released', { filename: 'x.wav' }, echo)).toBe(
      'notifications.fileTakedownReleasedMessage:{"filename":"x.wav"}'
    );
  });
});

describe('a task refused by the LLM token limits (#790)', () => {
  it.each([
    ['user_day', 'notifications.llmBudgetExceededDay'],
    ['instance_month', 'notifications.llmBudgetExceededMonth'],
  ])('translates the %s refusal instead of the English sentence', (scope, key) => {
    const data = {
      error_code: 'llm_budget_exceeded',
      budget_scope: scope,
      message: 'You have reached your daily AI token limit.',
    };
    expect(composeClientMessage('summarization_status', data, echo)).toBe(key);
  });

  it('translates an unchecked limit and leaves every other frame alone', () => {
    expect(
      composeClientMessage(
        'topic_extraction_status',
        { error_code: 'llm_budget_unavailable' },
        echo
      )
    ).toBe('notifications.llmBudgetUnavailable');
    expect(
      composeClientMessage('summarization_status', { error_code: 'topic_extraction_failed' }, echo)
    ).toBeUndefined();
  });
});
