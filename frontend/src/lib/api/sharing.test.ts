/**
 * `SharingApi` is a thin static-class CRUD wrapper around
 * `/collections/{uuid}/shares` — these tests pin request shape and response
 * pass-through.
 */
import { describe, it, expect, vi, beforeEach } from 'vitest';

const mockInstance = vi.hoisted(() => ({
  get: vi.fn(),
  post: vi.fn(),
  put: vi.fn(),
  delete: vi.fn(),
}));

vi.mock('../axios', () => ({ default: mockInstance }));

import { SharingApi } from './sharing';

const COLLECTION_UUID = '11111111-1111-1111-1111-111111111111';
const SHARE_UUID = '22222222-2222-2222-2222-222222222222';

beforeEach(() => {
  vi.clearAllMocks();
});

describe('SharingApi', () => {
  it('lists shares for a collection', async () => {
    const shares = [{ uuid: SHARE_UUID, target_type: 'user', permission: 'viewer' }];
    mockInstance.get.mockResolvedValue({ data: shares });

    const result = await SharingApi.fetchCollectionShares(COLLECTION_UUID);
    expect(mockInstance.get).toHaveBeenCalledWith(`/collections/${COLLECTION_UUID}/shares`);
    expect(result).toEqual(shares);
  });

  it('creates a share', async () => {
    const share = {
      uuid: SHARE_UUID,
      target_type: 'user',
      target_uuid: 'u1',
      permission: 'viewer',
    };
    mockInstance.post.mockResolvedValue({ data: share });

    const result = await SharingApi.shareCollection(COLLECTION_UUID, {
      target_type: 'user',
      target_uuid: 'u1',
    });
    expect(mockInstance.post).toHaveBeenCalledWith(`/collections/${COLLECTION_UUID}/shares`, {
      target_type: 'user',
      target_uuid: 'u1',
    });
    expect(result).toEqual(share);
  });

  it('updates a share permission', async () => {
    const share = { uuid: SHARE_UUID, permission: 'editor' };
    mockInstance.put.mockResolvedValue({ data: share });

    const result = await SharingApi.updateSharePermission(COLLECTION_UUID, SHARE_UUID, {
      permission: 'editor',
    });
    expect(mockInstance.put).toHaveBeenCalledWith(
      `/collections/${COLLECTION_UUID}/shares/${SHARE_UUID}`,
      { permission: 'editor' }
    );
    expect(result).toEqual(share);
  });

  it('revokes a share', async () => {
    mockInstance.delete.mockResolvedValue({ data: undefined });

    await expect(SharingApi.revokeShare(COLLECTION_UUID, SHARE_UUID)).resolves.toBeUndefined();
    expect(mockInstance.delete).toHaveBeenCalledWith(
      `/collections/${COLLECTION_UUID}/shares/${SHARE_UUID}`
    );
  });

  it('lists collections shared with the caller', async () => {
    const shared = [{ uuid: 'c1', name: 'Team Recordings', my_permission: 'viewer' }];
    mockInstance.get.mockResolvedValue({ data: shared });

    const result = await SharingApi.fetchSharedCollections();
    expect(mockInstance.get).toHaveBeenCalledWith('/collections/shared-with-me');
    expect(result).toEqual(shared);
  });
});
