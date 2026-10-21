import { describe, it, expect, vi, beforeEach } from 'vitest';

const mockInstance = vi.hoisted(() => ({ get: vi.fn(), post: vi.fn() }));
vi.mock('../axios', () => ({ default: mockInstance }));

import {
  cancelLocalModelDownload,
  getLocalModelCatalog,
  startLocalModelDownload,
} from './localModels';

beforeEach(() => vi.clearAllMocks());

describe('local model library client', () => {
  it('reads the catalog from /admin/local-models', async () => {
    const doc = {
      models: [],
      download: null,
      free_bytes: 1,
      cache_writable: true,
      active_model: 'x',
    };
    mockInstance.get.mockResolvedValue({ data: doc });
    await expect(getLocalModelCatalog()).resolves.toEqual(doc);
    expect(mockInstance.get).toHaveBeenCalledWith('/admin/local-models');
  });

  it('starts a download with exactly {model_id}', async () => {
    const state = { model_id: 'tiny', status: 'queued' };
    mockInstance.post.mockResolvedValue({ data: state });
    await expect(startLocalModelDownload('tiny')).resolves.toEqual(state);
    expect(mockInstance.post).toHaveBeenCalledWith('/admin/local-models/download', {
      model_id: 'tiny',
    });
  });

  it('cancels with a body-less POST to /admin/local-models/download/cancel', async () => {
    const state = { model_id: 'tiny', status: 'downloading' };
    mockInstance.post.mockResolvedValue({ data: state });
    await expect(cancelLocalModelDownload()).resolves.toEqual(state);
    expect(mockInstance.post).toHaveBeenCalledWith('/admin/local-models/download/cancel');
  });
});
