import { describe, it, expect, vi, beforeEach } from 'vitest';

const mockInstance = vi.hoisted(() => ({ get: vi.fn() }));
vi.mock('../axios', () => ({ default: mockInstance }));

import { getPipelineView } from './pipeline';
import { fullLocalView } from '$lib/settings/pipelineViewFixtures';

beforeEach(() => vi.clearAllMocks());

describe('getPipelineView', () => {
  it('reads the plan from GET /user-settings/pipeline', async () => {
    const view = fullLocalView();
    mockInstance.get.mockResolvedValue({ data: view });
    await expect(getPipelineView()).resolves.toEqual(view);
    expect(mockInstance.get).toHaveBeenCalledWith('/user-settings/pipeline');
  });

  it.each([{}, { steps: [] }, { issues: [] }, null])(
    'rejects a reply that is not a pipeline view (%j)',
    async (data) => {
      mockInstance.get.mockResolvedValue({ data });
      await expect(getPipelineView()).rejects.toThrow('Malformed');
    }
  );
});
