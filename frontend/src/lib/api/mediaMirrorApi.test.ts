/**
 * `mediaMirrorApi.ts` mirrors `backupApi.ts`'s shape for the incremental media
 * mirror feature. Each call's wire shape is asserted alongside the returned
 * payload passing through unchanged.
 */
import { describe, it, expect, vi, beforeEach } from 'vitest';

const mockInstance = vi.hoisted(() => ({ get: vi.fn(), post: vi.fn(), put: vi.fn() }));
vi.mock('$lib/axios', () => ({ default: mockInstance }));

import {
  getMediaMirrorSettings,
  updateMediaMirrorSettings,
  runMediaMirrorNow,
  testMirrorS3Connection,
} from './mediaMirrorApi';

beforeEach(() => {
  vi.clearAllMocks();
});

describe('getMediaMirrorSettings / updateMediaMirrorSettings', () => {
  it('fetches from /admin/backup/mirror and returns the settings', async () => {
    mockInstance.get.mockResolvedValue({ data: { enabled: true, running: false } });
    const settings = await getMediaMirrorSettings();
    expect(mockInstance.get).toHaveBeenCalledWith('/admin/backup/mirror');
    expect(settings.enabled).toBe(true);
  });

  it('PUTs the partial update and returns the merged settings', async () => {
    mockInstance.put.mockResolvedValue({ data: { enabled: false } });
    const result = await updateMediaMirrorSettings({ enabled: false });
    expect(mockInstance.put).toHaveBeenCalledWith('/admin/backup/mirror', { enabled: false });
    expect(result.enabled).toBe(false);
  });
});

describe('runMediaMirrorNow', () => {
  it('dispatches and returns the task id', async () => {
    mockInstance.post.mockResolvedValue({
      data: { task_id: 't1', status: 'queued', message: 'ok' },
    });
    const result = await runMediaMirrorNow();
    expect(mockInstance.post).toHaveBeenCalledWith('/admin/backup/mirror/run');
    expect(result.task_id).toBe('t1');
  });
});

describe('testMirrorS3Connection', () => {
  it('posts the S3 settings under test and returns whether the bucket is reachable', async () => {
    mockInstance.post.mockResolvedValue({ data: { ok: true, bucket: 'my-mirror-bucket' } });
    const result = await testMirrorS3Connection({ s3_bucket: 'my-mirror-bucket' });
    expect(mockInstance.post).toHaveBeenCalledWith('/admin/backup/mirror/test-s3', {
      s3_bucket: 'my-mirror-bucket',
    });
    expect(result.ok).toBe(true);
  });
});
