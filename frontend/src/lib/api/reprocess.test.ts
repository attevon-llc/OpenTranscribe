import { describe, it, expect, vi, beforeEach } from 'vitest';

const mockInstance = vi.hoisted(() => ({ post: vi.fn() }));
vi.mock('../axios', () => ({ default: mockInstance }));

import { reprocessFile, reprocessFiles } from './reprocess';

beforeEach(() => vi.clearAllMocks());

describe('reprocessFile', () => {
  it('posts the body to the file route', async () => {
    mockInstance.post.mockResolvedValue({ data: {} });
    await expect(reprocessFile('f1', { stages: ['analytics'] })).resolves.toBeUndefined();
    expect(mockInstance.post).toHaveBeenCalledWith('/files/f1/reprocess', {
      stages: ['analytics'],
    });
  });
  it('lets a refusal reach the caller', async () => {
    mockInstance.post.mockRejectedValue(new Error('409'));
    await expect(reprocessFile('f1', {})).rejects.toThrow('409');
  });
});

describe('reprocessFiles', () => {
  it('posts one bulk action naming the files and the reprocess action, and returns per-file results', async () => {
    mockInstance.post.mockResolvedValue({ data: [{ success: true }, { success: false }] });
    const results = await reprocessFiles(['a', 'b'], { stages: ['analytics'] });
    expect(mockInstance.post).toHaveBeenCalledWith('/files/management/bulk-action', {
      stages: ['analytics'],
      file_uuids: ['a', 'b'],
      action: 'reprocess',
    });
    expect(results).toEqual([{ success: true }, { success: false }]);
  });
  it('does not let the body override the action or the files', async () => {
    mockInstance.post.mockResolvedValue({ data: [] });
    await reprocessFiles(['a'], { action: 'delete', file_uuids: ['x'] });
    expect(mockInstance.post.mock.calls[0][1]).toMatchObject({
      action: 'reprocess',
      file_uuids: ['a'],
    });
  });
});
