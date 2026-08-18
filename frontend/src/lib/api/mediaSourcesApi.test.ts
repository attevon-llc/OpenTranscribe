/**
 * `mediaSourcesApi.ts` is a thin wrapper around `/user-settings/media-sources`.
 * `toggleMediaSourceShare` is worth its own case: it reuses the general update
 * endpoint with a single-field body rather than a dedicated route.
 */
import { describe, it, expect, vi, beforeEach } from 'vitest';

const mockInstance = vi.hoisted(() => ({
  get: vi.fn(),
  post: vi.fn(),
  put: vi.fn(),
  delete: vi.fn(),
}));
vi.mock('../axios', () => ({ default: mockInstance }));

import {
  getMediaSources,
  addMediaSource,
  updateMediaSource,
  deleteMediaSource,
  toggleMediaSourceShare,
} from './mediaSourcesApi';

beforeEach(() => {
  vi.clearAllMocks();
});

describe('getMediaSources', () => {
  it('returns own and shared sources', async () => {
    mockInstance.get.mockResolvedValue({
      data: { sources: [{ uuid: 's1' }], shared_sources: [{ uuid: 's2' }] },
    });
    const result = await getMediaSources();
    expect(mockInstance.get).toHaveBeenCalledWith('/user-settings/media-sources');
    expect(result.sources).toHaveLength(1);
    expect(result.shared_sources).toHaveLength(1);
  });
});

describe('addMediaSource / updateMediaSource / deleteMediaSource', () => {
  it('creates a source and returns the server row', async () => {
    mockInstance.post.mockResolvedValue({ data: { uuid: 's1', hostname: 'nas.local' } });
    const result = await addMediaSource({ hostname: 'nas.local' });
    expect(mockInstance.post).toHaveBeenCalledWith('/user-settings/media-sources', {
      hostname: 'nas.local',
    });
    expect(result.uuid).toBe('s1');
  });

  it('updates a source by uuid', async () => {
    mockInstance.put.mockResolvedValue({ data: { uuid: 's1', label: 'NAS' } });
    const result = await updateMediaSource('s1', { label: 'NAS' });
    expect(mockInstance.put).toHaveBeenCalledWith('/user-settings/media-sources/s1', {
      label: 'NAS',
    });
    expect(result.label).toBe('NAS');
  });

  it('deletes a source by uuid, resolving void rather than swallowing a rejection', async () => {
    await expect(deleteMediaSource('s1')).resolves.toBeUndefined();
    expect(mockInstance.delete).toHaveBeenCalledWith('/user-settings/media-sources/s1');
  });
});

describe('toggleMediaSourceShare', () => {
  it('PUTs only the is_shared field to the general update endpoint', async () => {
    mockInstance.put.mockResolvedValue({ data: { uuid: 's1', is_shared: true } });
    const result = await toggleMediaSourceShare('s1', true);
    expect(mockInstance.put).toHaveBeenCalledWith('/user-settings/media-sources/s1', {
      is_shared: true,
    });
    expect(result.is_shared).toBe(true);
  });
});
