/**
 * `prompts.ts` is a thin wrapper, but `getPrompts`/`getSharedLibrary` build their
 * query string by hand (append-if-defined) rather than passing a params object,
 * so it's worth pinning that an omitted filter is actually omitted from the URL
 * rather than sent as the literal string "undefined".
 */
import { describe, it, expect, vi, beforeEach } from 'vitest';

const mockInstance = vi.hoisted(() => ({
  get: vi.fn(),
  post: vi.fn(),
  put: vi.fn(),
  delete: vi.fn(),
}));
vi.mock('../axios', () => ({ default: mockInstance }));

import { PromptsApi } from './prompts';

beforeEach(() => {
  vi.clearAllMocks();
  mockInstance.get.mockResolvedValue({ data: {} });
  mockInstance.post.mockResolvedValue({ data: {} });
  mockInstance.put.mockResolvedValue({ data: {} });
  mockInstance.delete.mockResolvedValue({ data: {} });
});

describe('getPrompts', () => {
  it('builds an empty query string when no filters are given, and returns the list', async () => {
    mockInstance.get.mockResolvedValue({
      data: { prompts: [], total: 0, page: 1, size: 20, has_next: false, has_prev: false },
    });
    const result = await PromptsApi.getPrompts();
    expect(mockInstance.get).toHaveBeenCalledWith('/prompts?');
    expect(result.prompts).toEqual([]);
  });

  it('appends only the filters that were actually provided', async () => {
    mockInstance.get.mockResolvedValue({
      data: {
        prompts: [{ uuid: 'p1' }],
        total: 1,
        page: 1,
        size: 10,
        has_next: false,
        has_prev: false,
      },
    });
    const result = await PromptsApi.getPrompts({ include_system: true, limit: 10 });
    expect(mockInstance.get).toHaveBeenCalledWith('/prompts?include_system=true&limit=10');
    expect(result.total).toBe(1);
  });
});

describe('getSharedLibrary', () => {
  it('omits every param when called with no arguments, and returns the library payload', async () => {
    mockInstance.get.mockResolvedValue({
      data: {
        prompts: [],
        total: 0,
        page: 1,
        size: 20,
        has_next: false,
        has_prev: false,
        available_tags: ['ops'],
      },
    });
    const result = await PromptsApi.getSharedLibrary();
    expect(mockInstance.get).toHaveBeenCalledWith('/prompts/shared/library?');
    expect(result.available_tags).toEqual(['ops']);
  });

  it('includes every provided param', async () => {
    mockInstance.get.mockResolvedValue({
      data: {
        prompts: [{ uuid: 'p2' }],
        total: 1,
        page: 1,
        size: 10,
        has_next: false,
        has_prev: false,
        available_tags: [],
      },
    });
    const result = await PromptsApi.getSharedLibrary({ search: 'meeting', skip: 20, limit: 10 });
    expect(mockInstance.get).toHaveBeenCalledWith(
      '/prompts/shared/library?search=meeting&skip=20&limit=10'
    );
    expect(result.prompts).toEqual([{ uuid: 'p2' }]);
  });
});

describe('mutations', () => {
  it('creates, updates, and deletes against the right endpoints', async () => {
    mockInstance.post.mockResolvedValue({ data: { uuid: 'p1', name: 'New' } });
    const created = await PromptsApi.createPrompt({ name: 'New', prompt_text: '...' });
    expect(mockInstance.post).toHaveBeenCalledWith('/prompts', { name: 'New', prompt_text: '...' });
    expect(created.uuid).toBe('p1');

    await PromptsApi.updatePrompt('p1', { name: 'Renamed' });
    expect(mockInstance.put).toHaveBeenCalledWith('/prompts/p1', { name: 'Renamed' });

    await PromptsApi.deletePrompt('p1');
    expect(mockInstance.delete).toHaveBeenCalledWith('/prompts/p1');
  });

  it('sets the active prompt by uuid, resolving void rather than swallowing a rejection', async () => {
    await expect(PromptsApi.setActivePrompt({ prompt_id: 'p1' })).resolves.toBeUndefined();
    expect(mockInstance.post).toHaveBeenCalledWith('/prompts/active/set', { prompt_id: 'p1' });
  });

  it('toggles sharing and clones an accessible prompt', async () => {
    await PromptsApi.sharePrompt('p1', true);
    expect(mockInstance.post).toHaveBeenCalledWith('/prompts/shared/p1/toggle', {
      is_shared: true,
    });

    mockInstance.post.mockResolvedValue({ data: { uuid: 'p2' } });
    const cloned = await PromptsApi.clonePrompt('p1');
    expect(mockInstance.post).toHaveBeenCalledWith('/prompts/p1/clone');
    expect(cloned.uuid).toBe('p2');
  });
});
