import { describe, it, expect, vi, beforeEach } from 'vitest';

vi.mock('$lib/axios', () => ({
  default: { get: vi.fn() },
}));

import axiosInstance from '$lib/axios';
import { getSpeakerIdentityHistory, IDENTITY_HISTORY_LIMIT } from './speakerHistory';

const get = vi.mocked(axiosInstance.get);

beforeEach(() => {
  vi.clearAllMocks();
  get.mockResolvedValue({ data: [{ uuid: 'e-1' }] } as never);
});

describe('getSpeakerIdentityHistory', () => {
  it('reads the path the backend mounts, with the page size', async () => {
    const rows = await getSpeakerIdentityHistory('s-1');
    expect(get).toHaveBeenCalledWith('/speakers/s-1/identity-history', {
      params: { limit: IDENTITY_HISTORY_LIMIT },
    });
    expect(rows).toEqual([{ uuid: 'e-1' }]);
  });
});
