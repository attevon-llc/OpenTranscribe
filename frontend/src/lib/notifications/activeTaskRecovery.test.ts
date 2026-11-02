/**
 * Pins reconnect recovery of running tasks: the card key must match the one live frames
 * use (else a refresh forks a second card), the settings panel event must carry every
 * alias its listeners read (`processed_files`, `indexed_files` ...), and a task type no
 * surface shows must be skipped.
 */
import { describe, it, expect } from 'vitest';
import { formatEta } from '$lib/utils/formatting';
import { recoverTask, recoveredNotification, type ActiveTask } from './activeTaskRecovery';

const task = (over: Partial<ActiveTask> = {}): ActiveTask => ({
  task_type: 'reindex',
  user_id: 1,
  total: 8,
  processed: 2,
  status: 'running',
  message: 'Indexing',
  eta_seconds: 120,
  ...over,
});

describe('recoverTask', () => {
  it.each([
    ['reindex', 'reindex_progress', 'admin_reindex', 'reindex-progress'],
    ['migration', 'migration_progress', 'admin_migration', 'migration-progress'],
    [
      'attribute_migration',
      'attribute_migration_progress',
      'admin_attribute_migration',
      'attribute-migration-progress',
    ],
    ['clustering', 'clustering_progress', 'admin_clustering', 'clustering-progress'],
    ['auto_label', 'auto_label_status', 'auto_label_status_retroactive_apply', 'auto-label-status'],
    [
      'data_integrity',
      'data_integrity_progress',
      'admin_data_integrity',
      'data-integrity-progress',
    ],
    [
      'embedding_consistency',
      'embedding_consistency_progress',
      'admin_embedding_consistency',
      'embedding-consistency-progress',
    ],
  ])('%s -> %s card %s, event %s', (taskType, notificationType, progressId, eventName) => {
    const r = recoverTask(task({ task_type: taskType }));
    expect(r).toMatchObject({ notificationType, progressId });
    expect(r?.event?.name).toBe(eventName);
  });

  it('skips a task type nothing displays', () => {
    expect(recoverTask(task({ task_type: 'thumbnail_backfill' }))).toBeNull();
  });

  it('computes percent and ETA, and an event detail with every alias listeners read', () => {
    const r = recoverTask(task());
    expect(r?.percentage).toBe(25);
    expect(r?.etaDisplay).toBe(formatEta(120));
    expect(r?.event?.detail).toEqual({
      progress: 0.25,
      processed: 2,
      total: 8,
      processed_files: 2,
      total_files: 8,
      indexed_files: 2,
      message: 'Indexing',
      eta_seconds: 120,
      running: true,
      status: 'processing',
    });
  });

  it('reports 0 rather than NaN for a task with no total yet', () => {
    const r = recoverTask(task({ total: 0, processed: 0 }));
    expect(r?.percentage).toBe(0);
    expect(r?.event?.detail.progress).toBe(0);
  });
});

describe('recoveredNotification', () => {
  it('builds an unread, undismissable processing card', () => {
    const t = task();
    const r = recoverTask(t)!;
    const n = recoveredNotification(t, r, 'id-1', 'Title');
    expect(n).toMatchObject({
      id: 'id-1',
      progressId: 'admin_reindex',
      type: 'reindex_progress',
      title: 'Title',
      message: 'Indexing',
      currentStep: 'Indexing',
      read: false,
      status: 'processing',
      dismissible: false,
      data: { progress: 25, processed: 2, total: 8, eta_seconds: 120 },
      progress: {
        current: 25,
        total: 100,
        percentage: 25,
        etaSeconds: 120,
        etaDisplay: formatEta(120),
      },
    });
    expect(n.timestamp).toBeInstanceOf(Date);
  });
});
