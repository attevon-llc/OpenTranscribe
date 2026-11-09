import { beforeEach, describe, expect, it, vi } from 'vitest';

const { quarantineFile } = vi.hoisted(() => ({ quarantineFile: vi.fn() }));
vi.mock('$lib/api/admin', () => ({ AdminApi: { quarantineFile } }));

import { quarantineFiles } from './quarantineBatch';

const files = [{ uuid: 'a' }, { uuid: 'b' }, { uuid: 'c' }];
const basis = { operatorActionReason: 'abuse' };

beforeEach(() => {
  vi.clearAllMocks();
  vi.spyOn(console, 'error').mockImplementation(() => {});
});

describe('quarantineFiles', () => {
  it('runs one call per file, in order, one at a time', async () => {
    const order: string[] = [];
    let inFlight = 0;
    let peak = 0;
    quarantineFile.mockImplementation(async (uuid: string) => {
      inFlight += 1;
      peak = Math.max(peak, inFlight);
      await Promise.resolve();
      order.push(uuid);
      inFlight -= 1;
      return {};
    });
    const result = await quarantineFiles(files, 'why', true, basis);
    expect(order).toEqual(['a', 'b', 'c']);
    expect(peak).toBe(1);
    expect(result).toEqual({ succeededUuids: ['a', 'b', 'c'], failedCount: 0, unrevokedCount: 0 });
    expect(quarantineFile).toHaveBeenCalledWith('a', 'why', true, basis);
  });

  it('carries on past a failed file and counts it, without throwing', async () => {
    quarantineFile
      .mockResolvedValueOnce({})
      .mockRejectedValueOnce(new Error('403'))
      .mockResolvedValueOnce({});
    const result = await quarantineFiles(files, 'why', false, basis);
    expect(result.succeededUuids).toEqual(['a', 'c']);
    expect(result.failedCount).toBe(1);
  });

  it('counts a takedown whose link revocation was incomplete as succeeded but unrevoked', async () => {
    quarantineFile
      .mockResolvedValueOnce({ media_revocation: { complete: false } })
      .mockResolvedValue({});
    const result = await quarantineFiles(files, 'why', false, basis);
    expect(result.succeededUuids).toHaveLength(3);
    expect(result.unrevokedCount).toBe(1);
  });
});
