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
  retryWatchSourceFiles,
  bulkDeleteWatchSourceFiles,
  getEmailLinks,
  getAvailableEmailConfigs,
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
      params: { page: 1, page_size: 50, status: undefined, q: undefined },
    });
    expect(result.total).toBe(0);
  });

  it('sends the status and search filters as params, not a hand-built query string', async () => {
    // Both filters are server-side by design: a source can track more files than a
    // client should ever hold, so filtering here would mean paging the whole table
    // into the browser to hide most of it.
    mockInstance.get.mockResolvedValue({
      data: {
        files: [
          { uuid: 'f1', filename: '2026-board-meeting.mp4', status: 'error', retry_count: 1 },
        ],
        total: 1,
        page: 2,
        page_size: 50,
      },
    });
    const result = await getWatchSourceFiles('w1', 2, 50, 'error', 'board');
    expect(mockInstance.get).toHaveBeenCalledWith('/watch-sources/w1/files', {
      params: { page: 2, page_size: 50, status: 'error', q: 'board' },
    });
    // The envelope is returned intact — the table pages off `total`/`page`, so a
    // client that sent the right query and dropped the paging fields would still
    // break it.
    expect(result.total).toBe(1);
    expect(result.page).toBe(2);
    expect(result.files[0].filename).toBe('2026-board-meeting.mp4');
  });
});

describe('retryWatchSourceFiles', () => {
  it('posts every uuid in ONE request so the backend dispatches one scan', async () => {
    // The property the batch shape exists for: `scan_single` holds a Redis lock per
    // source, so three separate calls would run one scan and silently drop two.
    mockInstance.post.mockResolvedValue({ data: { results: [], scan_dispatched: true } });
    const result = await retryWatchSourceFiles('w1', ['f1', 'f2', 'f3']);
    expect(mockInstance.post).toHaveBeenCalledTimes(1);
    expect(mockInstance.post).toHaveBeenCalledWith('/watch-sources/w1/files/retry', {
      file_uuids: ['f1', 'f2', 'f3'],
    });
    expect(result.scan_dispatched).toBe(true);
  });

  it('returns the per-row results so a partial batch can be reported honestly', async () => {
    mockInstance.post.mockResolvedValue({
      data: {
        results: [
          { file_uuid: 'f1', success: true, status: 'pending' },
          {
            file_uuid: 'f2',
            success: false,
            error: "A file in state 'imported' cannot be retried",
          },
        ],
        scan_dispatched: true,
      },
    });
    const result = await retryWatchSourceFiles('w1', ['f1', 'f2']);
    expect(result.results.map((r) => r.success)).toEqual([true, false]);
    expect(result.results[1].error).toContain('cannot be retried');
  });
});

describe('bulkDeleteWatchSourceFiles', () => {
  it('posts the uuids to the bulk-delete route', async () => {
    mockInstance.post.mockResolvedValue({ data: { results: [], scan_dispatched: false } });
    const result = await bulkDeleteWatchSourceFiles('w1', ['f1', 'f2']);
    expect(mockInstance.post).toHaveBeenCalledWith('/watch-sources/w1/files/bulk-delete', {
      file_uuids: ['f1', 'f2'],
    });
    expect(result.scan_dispatched).toBe(false);
  });
});

describe('getEmailLinks', () => {
  it('returns each link with its own notify options', async () => {
    mockInstance.get.mockResolvedValue({
      data: [
        {
          email_config_uuid: 'e1',
          email_config_name: 'Ops mailer',
          additional_recipients: 'oncall@example.com',
          notify_on_success: false,
          notify_on_error: true,
        },
      ],
    });
    const links = await getEmailLinks('w1');
    expect(mockInstance.get).toHaveBeenCalledWith('/watch-sources/w1/emails');
    expect(links[0].notify_on_success).toBe(false);
    expect(links[0].notify_on_error).toBe(true);
  });

  it('answers an empty array when the body is null', async () => {
    // A source with no links is an ordinary state, not an error — returning undefined
    // here would make every caller guard before iterating.
    mockInstance.get.mockResolvedValue({ data: null });
    await expect(getEmailLinks('w1')).resolves.toEqual([]);
  });
});

describe('getAvailableEmailConfigs', () => {
  it('reads the source-scoped picker route, not the super_admin config list', async () => {
    // Linking is owner-level while `/email-configs` is super_admin, so an ordinary
    // owner can only populate this picker from the source-scoped route.
    mockInstance.get.mockResolvedValue({
      data: [
        {
          uuid: 'e1',
          name: 'Ops mailer',
          provider: 'smtp',
          is_enabled: true,
          has_default_recipients: false,
        },
      ],
    });
    const options = await getAvailableEmailConfigs('w1');
    expect(mockInstance.get).toHaveBeenCalledWith('/watch-sources/w1/emails/available');
    expect(options[0].has_default_recipients).toBe(false);
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
