/**
 * Pins how a realtime frame becomes a progress card: which frames are progress at all,
 * the card's grouping key (one card per file, per playlist, or per admin task), its
 * status, the step text synthesised for admin tasks that send none, and the 0-1 vs
 * 0-100 progress normalisation. A wrong key forks one task into many cards; a wrong
 * scale shows an admin task stuck at 1%.
 */
import { describe, it, expect } from 'vitest';
import type { Translate } from '$lib/i18n';
import { formatEta } from '$lib/utils/formatting';
import { formatEtaSeconds, frameKind, progressFields } from './progressFrame';

const echo: Translate = (key, opts) => (opts ? `${key}:${JSON.stringify(opts)}` : key);

describe('frameKind', () => {
  it('files per-file task frames as progress only when they name a file', () => {
    expect(frameKind('transcription_status', { file_id: 'f1' })).toBe('progress');
    expect(frameKind('summarization_status', { file_id: 'f1' })).toBe('progress');
    expect(frameKind('redaction_status', { file_id: 'f1' })).toBe('progress');
    expect(frameKind('topic_extraction_status', { file_id: 'f1' })).toBe('progress');
    expect(frameKind('auto_label_status', { file_id: 'f1' })).toBe('progress');
    expect(frameKind('youtube_processing_status', { file_id: 'f1' })).toBe('progress');
    expect(frameKind('transcription_status', {})).toBe('regular');
  });

  it('files playlist frames as progress without a file id', () => {
    expect(frameKind('playlist_processing_status', {})).toBe('progress');
  });

  it('files every admin task frame as progress', () => {
    for (const type of [
      'reindex_progress',
      'reindex_complete',
      'reindex_stopped',
      'migration_progress',
      'migration_complete',
      'migration_finalized',
      'clustering_progress',
      'clustering_complete',
      'attribute_migration_progress',
      'attribute_migration_complete',
      'data_integrity_progress',
      'data_integrity_complete',
      'embedding_consistency_progress',
      'embedding_consistency_complete',
    ]) {
      expect(frameKind(type, {}), type).toBe('progress');
    }
  });

  it('separates post-processing, silent and regular frames', () => {
    expect(frameKind('post_processing_status', {})).toBe('post_processing');
    for (const type of [
      'file_created',
      'file_updated',
      'clustering_file_complete',
      'transcript_ready',
    ]) {
      expect(frameKind(type, {}), type).toBe('silent');
    }
    expect(frameKind('file_deleted', {})).toBe('regular');
    expect(frameKind('file_takedown', {})).toBe('regular');
  });
});

describe('progressFields: grouping key and status', () => {
  it('keys a per-file task by type and file', () => {
    const f = progressFields('transcription_status', { file_id: 'f1', status: 'queued' }, echo);
    expect(f.progressId).toBe('transcription_status_f1');
    expect(f.status).toBe('queued');
  });

  it('defaults a per-file status to processing', () => {
    expect(progressFields('summarization_status', { file_id: 'f1' }, echo).status).toBe(
      'processing'
    );
  });

  it('keys a playlist by playlist id, or "unknown"', () => {
    expect(
      progressFields('playlist_processing_status', { playlist_id: 'p9' }, echo).progressId
    ).toBe('playlist_processing_p9');
    expect(progressFields('playlist_processing_status', {}, echo).progressId).toBe(
      'playlist_processing_unknown'
    );
  });

  it('keys admin tasks per task family and ignores the frame status', () => {
    const p = progressFields('reindex_progress', { status: 'completed' }, echo);
    expect(p.progressId).toBe('admin_reindex');
    expect(p.status).toBe('processing');
    expect(progressFields('reindex_stopped', {}, echo).status).toBe('completed');
    expect(progressFields('migration_finalized', {}, echo).progressId).toBe('admin_migration');
    expect(progressFields('embedding_consistency_complete', {}, echo)).toMatchObject({
      progressId: 'admin_embedding_consistency',
      status: 'completed',
    });
  });
});

describe('progressFields: step text', () => {
  it('keeps the frame message when there is one', () => {
    expect(progressFields('reindex_progress', { message: 'hi' }, echo).currentStep).toBe('hi');
  });

  it('synthesises reindex text, reading stats as a fallback', () => {
    expect(
      progressFields('reindex_progress', { stats: { indexed_files: 3, total_files: 9 } }, echo)
        .currentStep
    ).toBe('notifications.progress.indexedProgress:{"count":3,"total":9}');
    expect(progressFields('reindex_complete', { indexed_files: 4 }, echo).currentStep).toBe(
      'notifications.progress.indexedComplete:{"count":4}'
    );
  });

  it('synthesises migration and attribute-migration text', () => {
    expect(
      progressFields('migration_progress', { processed_files: 1, total_files: 2 }, echo).currentStep
    ).toBe('notifications.progress.processedProgress:{"count":1,"total":2}');
    expect(
      progressFields('attribute_migration_complete', { processed_files: 5 }, echo).currentStep
    ).toBe('notifications.progress.processedComplete:{"count":5}');
  });

  it('synthesises clustering text', () => {
    expect(
      progressFields('clustering_progress', { step: 2, total_steps: 5 }, echo).currentStep
    ).toBe('notifications.progress.clusteringStep:{"step":2,"total":5}');
    expect(progressFields('clustering_complete', {}, echo).currentStep).toBe(
      'notifications.progress.clusteringComplete'
    );
  });

  it('synthesises data-integrity text, reading processed/total as a fallback', () => {
    expect(
      progressFields('data_integrity_progress', { processed: 7, total: 8 }, echo).currentStep
    ).toBe('notifications.progress.checkedProgress:{"count":7,"total":8}');
    expect(
      progressFields('data_integrity_complete', { processed_files: 6 }, echo).currentStep
    ).toBe('notifications.progress.checkedComplete:{"count":6}');
  });

  it('synthesises embedding-consistency text from the repaired count', () => {
    expect(
      progressFields('embedding_consistency_progress', { processed_files: 1, total_files: 3 }, echo)
        .currentStep
    ).toBe('notifications.progress.repairingProgress:{"count":1,"total":3}');
    expect(
      progressFields('embedding_consistency_complete', { repaired: 2 }, echo).currentStep
    ).toBe('notifications.progress.repairedComplete:{"count":2}');
  });

  it('falls back to the generic processing text for a per-file task with no message', () => {
    expect(progressFields('redaction_status', { file_id: 'f' }, echo).currentStep).toBe(
      'notifications.progress.processing'
    );
  });
});

describe('progressFields: progress and ETA', () => {
  it('scales admin 0-1 progress to percent', () => {
    expect(progressFields('reindex_progress', { progress: 0.256 }, echo).progress).toEqual({
      current: 25,
      total: 100,
      percentage: 26,
      etaSeconds: null,
      etaDisplay: undefined,
    });
    expect(progressFields('migration_progress', { progress: 1 }, echo).progress.percentage).toBe(
      100
    );
    expect(progressFields('migration_progress', { progress: 40 }, echo).progress.percentage).toBe(
      40
    );
  });

  it('keeps per-file progress as sent (already 0-100)', () => {
    expect(
      progressFields('transcription_status', { file_id: 'f', progress: 0.5 }, echo).progress
        .percentage
    ).toBe(1);
    expect(
      progressFields('transcription_status', { file_id: 'f', progress: 42.4 }, echo).progress
    ).toMatchObject({ current: 42, percentage: 42 });
  });

  it('carries the ETA and its display text', () => {
    const p = progressFields('transcription_status', { file_id: 'f', eta_seconds: 90 }, echo);
    expect(p.progress.etaSeconds).toBe(90);
    expect(p.progress.etaDisplay).toBe(formatEta(90));
    expect(p.progress.etaDisplay).toBeTruthy();
  });

  it('formatEtaSeconds returns undefined, not an empty string, without an ETA', () => {
    expect(formatEtaSeconds(null)).toBeUndefined();
    expect(formatEtaSeconds(undefined)).toBeUndefined();
  });
});

describe('a progressive frame refused by the LLM token limits (#790)', () => {
  it('shows the translated refusal, not the backend sentence', () => {
    const f = progressFields(
      'summarization_status',
      {
        file_id: 'f1',
        status: 'failed',
        message: 'You have reached your daily AI token limit.',
        error_code: 'llm_budget_exceeded',
        budget_scope: 'user_day',
      },
      echo
    );
    expect(f.currentStep).toBe('notifications.llmBudgetExceededDay');
  });
});
