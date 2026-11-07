import { describe, it, expect, vi, beforeEach } from 'vitest';

const axios = vi.hoisted(() => ({ put: vi.fn(), delete: vi.fn(), post: vi.fn() }));
vi.mock('$lib/axios', () => ({ default: axios }));

import { HiddenMediaApi } from './hiddenMedia';

beforeEach(() => vi.clearAllMocks());

describe('HiddenMediaApi (#1102)', () => {
  it('hides with PUT and unhides with DELETE on the file', async () => {
    await HiddenMediaApi.setHidden('f1', true);
    await HiddenMediaApi.setHidden('f1', false);
    expect(axios.put).toHaveBeenCalledWith('/files/f1/hidden');
    expect(axios.delete).toHaveBeenCalledWith('/files/f1/hidden');
  });

  it('sends the bulk verb for many files and returns the per-file results', async () => {
    axios.post.mockResolvedValue({ data: [{ file_uuid: 'a', success: true, message: 'ok' }] });
    const results = await HiddenMediaApi.setHiddenMany(['a', 'b'], false);
    expect(axios.post).toHaveBeenCalledWith('/files/management/bulk-action', {
      file_uuids: ['a', 'b'],
      action: 'unhide',
    });
    expect(results).toEqual([{ file_uuid: 'a', success: true, message: 'ok' }]);
  });
});
