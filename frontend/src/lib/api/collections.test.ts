/**
 * `CollectionsApi.setVisibility` is the only caller of `PATCH /collections/{uuid}/visibility`
 * (issue #1275). Pin the request shape and that the server's answer is passed through whole,
 * because `can_change_visibility` is computed there and the UI must not re-derive it.
 */
import { describe, it, expect, vi, beforeEach } from 'vitest';

const mockInstance = vi.hoisted(() => ({ patch: vi.fn() }));

vi.mock('../axios', () => ({ default: mockInstance }));

import { CollectionsApi } from './collections';

const UUID = '11111111-1111-1111-1111-111111111111';

beforeEach(() => {
  vi.clearAllMocks();
});

describe('CollectionsApi.setVisibility', () => {
  it('PATCHes the visibility route with only the mode', async () => {
    const updated = { uuid: UUID, visibility: 'restricted', can_change_visibility: true };
    mockInstance.patch.mockResolvedValue({ data: updated });

    const result = await CollectionsApi.setVisibility(UUID, 'restricted');

    expect(mockInstance.patch).toHaveBeenCalledWith(`/collections/${UUID}/visibility`, {
      visibility: 'restricted',
    });
    expect(result).toEqual(updated);
  });

  it('lets a refusal reach the caller instead of swallowing it', async () => {
    mockInstance.patch.mockRejectedValue({ response: { status: 403 } });

    await expect(CollectionsApi.setVisibility(UUID, 'organization')).rejects.toEqual({
      response: { status: 403 },
    });
  });
});
