import { describe, it, expect, vi, beforeEach } from 'vitest';

vi.mock('$lib/axios', () => {
  const axiosInstance = { post: vi.fn() };
  return { default: axiosInstance };
});

import axiosInstance from '$lib/axios';
import { BULK_ACTION_MAX_FILES, postBulkAction } from './bulkActions';

const post = vi.mocked(axiosInstance.post);

function uuids(n: number): string[] {
  return Array.from({ length: n }, (_, i) => `file-${i}`);
}

describe('postBulkAction', () => {
  beforeEach(() => {
    vi.clearAllMocks();
    // Echo one result per file sent, so ordering and completeness are observable.
    post.mockImplementation(async (_url: string, body: unknown) => {
      const sent = (body as { file_uuids: string[] }).file_uuids;
      return { data: sent.map((file_uuid) => ({ file_uuid, success: true })) } as never;
    });
  });

  it('never sends more files in one request than the server accepts', async () => {
    const all = uuids(BULK_ACTION_MAX_FILES * 2 + 7);

    const results = await postBulkAction<{ file_uuid: string }>({
      file_uuids: all,
      action: 'summarize',
    });

    const sizes = post.mock.calls.map(
      (call) => (call[1] as { file_uuids: string[] }).file_uuids.length
    );
    expect(sizes).toEqual([BULK_ACTION_MAX_FILES, BULK_ACTION_MAX_FILES, 7]);
    expect(results.map((r) => r.file_uuid)).toEqual(all);
  });

  it('sends every other field with each batch', async () => {
    await postBulkAction({
      file_uuids: uuids(BULK_ACTION_MAX_FILES + 1),
      action: 'add_tag',
      tag_name: 'x',
    });

    for (const call of post.mock.calls) {
      expect(call[0]).toBe('/files/management/bulk-action');
      expect(call[1]).toMatchObject({ action: 'add_tag', tag_name: 'x' });
    }
    expect(post).toHaveBeenCalledTimes(2);
  });

  it('a selection within the cap is one request', async () => {
    await postBulkAction({ file_uuids: uuids(3), action: 'hide' });
    expect(post).toHaveBeenCalledTimes(1);
  });
});
