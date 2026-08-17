/**
 * `backupApi.ts` is a thin typed wrapper around `/admin/backup`. Each call's wire
 * shape is asserted alongside the returned payload passing through unchanged.
 */
import { describe, it, expect, vi, beforeEach } from 'vitest';

const mockInstance = vi.hoisted(() => ({ get: vi.fn(), post: vi.fn(), put: vi.fn() }));
vi.mock('$lib/axios', () => ({ default: mockInstance }));

import {
  getBackupSettings,
  updateBackupSettings,
  getBackupStatus,
  runBackupNow,
  listBackups,
  testS3Connection,
} from './backupApi';

beforeEach(() => {
  vi.clearAllMocks();
});

describe('getBackupSettings / updateBackupSettings', () => {
  it('fetches from /admin/backup and returns the settings', async () => {
    mockInstance.get.mockResolvedValue({ data: { enabled: true, schedule: '0 3 * * *' } });
    const settings = await getBackupSettings();
    expect(mockInstance.get).toHaveBeenCalledWith('/admin/backup');
    expect(settings.enabled).toBe(true);
  });

  it('PUTs the partial update and returns the merged settings', async () => {
    mockInstance.put.mockResolvedValue({ data: { enabled: false } });
    const result = await updateBackupSettings({ enabled: false });
    expect(mockInstance.put).toHaveBeenCalledWith('/admin/backup', { enabled: false });
    expect(result.enabled).toBe(false);
  });
});

describe('getBackupStatus', () => {
  it('reports the due state and destination health', async () => {
    mockInstance.get.mockResolvedValue({ data: { next_due: true, pg_dump_available: true } });
    const status = await getBackupStatus();
    expect(mockInstance.get).toHaveBeenCalledWith('/admin/backup/status');
    expect(status.next_due).toBe(true);
  });
});

describe('runBackupNow', () => {
  it('dispatches and returns the task id', async () => {
    mockInstance.post.mockResolvedValue({
      data: { task_id: 't1', status: 'queued', message: 'ok' },
    });
    const result = await runBackupNow();
    expect(mockInstance.post).toHaveBeenCalledWith('/admin/backup/run');
    expect(result.task_id).toBe('t1');
  });
});

describe('listBackups', () => {
  it('returns the backup list with destination status', async () => {
    mockInstance.get.mockResolvedValue({
      data: { backups: [{ filename: 'a.sql.gz' }], destination_status: { exists: true } },
    });
    const result = await listBackups();
    expect(mockInstance.get).toHaveBeenCalledWith('/admin/backup/list');
    expect(result.backups).toHaveLength(1);
  });
});

describe('testS3Connection', () => {
  it('posts the S3 settings under test and returns whether the bucket is reachable', async () => {
    mockInstance.post.mockResolvedValue({ data: { ok: true, bucket: 'my-bucket' } });
    const result = await testS3Connection({ s3_bucket: 'my-bucket' });
    expect(mockInstance.post).toHaveBeenCalledWith('/admin/backup/test-s3', {
      s3_bucket: 'my-bucket',
    });
    expect(result.ok).toBe(true);
  });
});
