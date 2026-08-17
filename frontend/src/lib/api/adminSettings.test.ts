/**
 * `adminSettings.ts` (`AdminSettingsApi`) is a thin wrapper over retry-config and
 * garbage-cleanup-config admin endpoints. Each call's wire shape is asserted
 * alongside the returned payload passing through unchanged.
 */
import { describe, it, expect, vi, beforeEach } from 'vitest';

const mockInstance = vi.hoisted(() => ({ get: vi.fn(), post: vi.fn(), put: vi.fn() }));
vi.mock('../axios', () => ({ default: mockInstance }));

import { AdminSettingsApi } from './adminSettings';

beforeEach(() => {
  vi.clearAllMocks();
});

describe('retry config', () => {
  it('gets and updates the retry configuration', async () => {
    mockInstance.get.mockResolvedValue({
      data: { max_retries: 3, retry_limit_enabled: true },
    });
    const config = await AdminSettingsApi.getRetryConfig();
    expect(mockInstance.get).toHaveBeenCalledWith('/admin/settings/retry-config');
    expect(config.max_retries).toBe(3);

    mockInstance.put.mockResolvedValue({ data: { max_retries: 5, retry_limit_enabled: true } });
    const updated = await AdminSettingsApi.updateRetryConfig({ max_retries: 5 });
    expect(mockInstance.put).toHaveBeenCalledWith('/admin/settings/retry-config', {
      max_retries: 5,
    });
    expect(updated.max_retries).toBe(5);
  });
});

describe('resetFileRetries', () => {
  it('posts against the FILE endpoint, not the admin settings path', async () => {
    mockInstance.post.mockResolvedValue({
      data: {
        message: 'reset',
        file_uuid: 'f1',
        filename: 'a.mp3',
        old_retry_count: 3,
        new_retry_count: 0,
        max_retries: 3,
      },
    });
    const result = await AdminSettingsApi.resetFileRetries('f1');
    expect(mockInstance.post).toHaveBeenCalledWith('/files/f1/reset-retries');
    expect(result.new_retry_count).toBe(0);
  });
});

describe('garbage cleanup config', () => {
  it('gets and updates the garbage cleanup configuration', async () => {
    mockInstance.get.mockResolvedValue({
      data: { garbage_cleanup_enabled: true, max_word_length: 50 },
    });
    const config = await AdminSettingsApi.getGarbageCleanupConfig();
    expect(mockInstance.get).toHaveBeenCalledWith('/admin/settings/garbage-cleanup');
    expect(config.max_word_length).toBe(50);

    mockInstance.put.mockResolvedValue({
      data: { garbage_cleanup_enabled: false, max_word_length: 50 },
    });
    const updated = await AdminSettingsApi.updateGarbageCleanupConfig({
      garbage_cleanup_enabled: false,
    });
    expect(mockInstance.put).toHaveBeenCalledWith('/admin/settings/garbage-cleanup', {
      garbage_cleanup_enabled: false,
    });
    expect(updated.garbage_cleanup_enabled).toBe(false);
  });
});
