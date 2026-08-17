import { describe, it, expect, vi, beforeEach } from 'vitest';

const mockInstance = vi.hoisted(() => ({
  get: vi.fn(),
  post: vi.fn(),
  put: vi.fn(),
  delete: vi.fn(),
}));
vi.mock('$lib/axios', () => ({ default: mockInstance }));

import {
  getSourceTypeLabel,
  getWatchSources,
  createWatchSource,
  deleteWatchSource,
  getWatchSourceFiles,
  linkEmailConfig,
} from './watchSourcesApi';

describe('watchSourcesApi', () => {
  it('maps source types to human labels', () => {
    expect(getSourceTypeLabel('local')).toBe('Local Folder');
    expect(getSourceTypeLabel('s3')).toBe('S3 Bucket');
    expect(getSourceTypeLabel('smb')).toBe('SMB Share');
  });

  it('falls back to the raw type for unknown values', () => {
    // @ts-expect-error testing the fallback path with an out-of-union value
    expect(getSourceTypeLabel('ftp')).toBe('ftp');
  });
});

beforeEach(() => {
  vi.clearAllMocks();
});

describe('getWatchSources', () => {
  it('defaults to scope "own" and unwraps the sources array', async () => {
    mockInstance.get.mockResolvedValue({ data: { sources: [{ uuid: 'w1' }] } });
    const result = await getWatchSources();
    expect(mockInstance.get).toHaveBeenCalledWith('/watch-sources', { params: { scope: 'own' } });
    expect(result).toEqual([{ uuid: 'w1' }]);
  });

  it('tolerates a response with no sources key rather than throwing', async () => {
    mockInstance.get.mockResolvedValue({ data: {} });
    expect(await getWatchSources('all')).toEqual([]);
    expect(mockInstance.get).toHaveBeenCalledWith('/watch-sources', { params: { scope: 'all' } });
  });
});

describe('createWatchSource / deleteWatchSource', () => {
  it('creates a source and returns the server row', async () => {
    mockInstance.post.mockResolvedValue({ data: { uuid: 'w1', name: 'NAS' } });
    const result = await createWatchSource({ name: 'NAS', source_type: 'local' });
    expect(mockInstance.post).toHaveBeenCalledWith('/watch-sources', {
      name: 'NAS',
      source_type: 'local',
    });
    expect(result.uuid).toBe('w1');
  });

  it('deletes a source by uuid, resolving void rather than swallowing a rejection', async () => {
    await expect(deleteWatchSource('w1')).resolves.toBeUndefined();
    expect(mockInstance.delete).toHaveBeenCalledWith('/watch-sources/w1');
  });
});

describe('getWatchSourceFiles', () => {
  it('defaults page/pageSize and omits an undefined status filter', async () => {
    mockInstance.get.mockResolvedValue({ data: { files: [], total: 0, page: 1, page_size: 50 } });
    const result = await getWatchSourceFiles('w1');
    expect(mockInstance.get).toHaveBeenCalledWith('/watch-sources/w1/files', {
      params: { page: 1, page_size: 50, status: undefined },
    });
    expect(result.total).toBe(0);
  });
});

describe('linkEmailConfig', () => {
  it('posts the link payload and resolves void', async () => {
    await expect(
      linkEmailConfig('w1', { email_config_uuid: 'e1', notify_on_error: true })
    ).resolves.toBeUndefined();
    expect(mockInstance.post).toHaveBeenCalledWith('/watch-sources/w1/emails', {
      email_config_uuid: 'e1',
      notify_on_error: true,
    });
  });
});
