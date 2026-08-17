/**
 * `directorySyncApi.ts` is a thin transport wrapper around
 * `/admin/directory-sync` — these tests pin the request shape (method, path,
 * body) and that the response body passes through unchanged.
 */
import { describe, it, expect, vi, beforeEach } from 'vitest';

const mockInstance = vi.hoisted(() => ({
  get: vi.fn(),
  put: vi.fn(),
  post: vi.fn(),
}));

vi.mock('$lib/axios', () => ({ default: mockInstance }));

import {
  getDirectorySyncSettings,
  updateDirectorySyncSettings,
  getDirectorySyncStatus,
  runDirectorySyncNow,
} from './directorySyncApi';

beforeEach(() => {
  vi.clearAllMocks();
});

describe('directorySyncApi', () => {
  it('fetches settings from the base route', async () => {
    const settings = {
      enabled: true,
      schedule: '0 3 * * *',
      dry_run: false,
      max_disables_per_run: 50,
    };
    mockInstance.get.mockResolvedValue({ data: settings });

    const result = await getDirectorySyncSettings();
    expect(mockInstance.get).toHaveBeenCalledWith('/admin/directory-sync');
    expect(result).toEqual(settings);
  });

  it('PUTs an update and returns the server row', async () => {
    const updated = {
      enabled: false,
      schedule: '0 3 * * *',
      dry_run: true,
      max_disables_per_run: 10,
    };
    mockInstance.put.mockResolvedValue({ data: updated });

    const result = await updateDirectorySyncSettings({ enabled: false, dry_run: true });
    expect(mockInstance.put).toHaveBeenCalledWith('/admin/directory-sync', {
      enabled: false,
      dry_run: true,
    });
    expect(result).toEqual(updated);
  });

  it('fetches status from the /status sub-route', async () => {
    const status = {
      enabled: true,
      schedule: '0 3 * * *',
      dry_run: false,
      max_disables_per_run: 50,
      next_due: true,
    };
    mockInstance.get.mockResolvedValue({ data: status });

    const result = await getDirectorySyncStatus();
    expect(mockInstance.get).toHaveBeenCalledWith('/admin/directory-sync/status');
    expect(result).toEqual(status);
  });

  it('triggers a manual run via POST /run', async () => {
    const runResponse = { task_id: 'task-1', status: 'queued', message: 'started' };
    mockInstance.post.mockResolvedValue({ data: runResponse });

    const result = await runDirectorySyncNow();
    expect(mockInstance.post).toHaveBeenCalledWith('/admin/directory-sync/run');
    expect(result).toEqual(runResponse);
  });
});
