import { describe, it, expect, vi, beforeEach } from 'vitest';

/**
 * getAISuggestions normalises a raw backend payload (optional fields, alternate id keys)
 * into the typed AISuggestions shape. The critical case here is `confidence`: 0 is a real,
 * meaningful score ("very low confidence"), not an absent value, so the fallback to the
 * "unknown" default of 0.5 must trigger only on null/undefined, never on 0.
 */
const mockInstance = vi.hoisted(() => ({
  get: vi.fn(),
  post: vi.fn(),
}));

vi.mock('$lib/axios', async () => {
  const actual = await vi.importActual<typeof import('$lib/axios')>('$lib/axios');
  return { ...actual, default: mockInstance };
});

import { getAISuggestions, extractAISuggestions } from './suggestions';

beforeEach(() => {
  vi.clearAllMocks();
});

describe('getAISuggestions', () => {
  it('requests the suggestions endpoint for the given file and returns the mapped result', async () => {
    mockInstance.get.mockResolvedValue({
      data: { suggested_tags: [], suggested_collections: [], status: 'pending', uuid: 'sug-1' },
    });
    const result = await getAISuggestions('file-uuid');
    expect(mockInstance.get).toHaveBeenCalledWith('/files/file-uuid/suggestions');
    expect(result?.suggestion_id).toBe('sug-1');
  });

  it('preserves a real confidence of 0 rather than falling back to 0.5', async () => {
    mockInstance.get.mockResolvedValue({
      data: {
        suggested_tags: [{ name: 'low-signal', confidence: 0 }],
        suggested_collections: [{ name: 'maybe-later', confidence: 0 }],
        status: 'pending',
        uuid: 'sug-1',
      },
    });
    const result = await getAISuggestions('file-uuid');
    expect(result?.tags[0].confidence).toBe(0);
    expect(result?.collections[0].confidence).toBe(0);
  });

  it('defaults confidence to 0.5 when the backend omits it entirely', async () => {
    mockInstance.get.mockResolvedValue({
      data: {
        suggested_tags: [{ name: 'unscored-tag' }],
        suggested_collections: [{ name: 'unscored-collection' }],
        status: 'pending',
        uuid: 'sug-1',
      },
    });
    const result = await getAISuggestions('file-uuid');
    expect(result?.tags[0].confidence).toBe(0.5);
    expect(result?.collections[0].confidence).toBe(0.5);
  });

  it('defaults suggested_tags/suggested_collections to empty arrays when missing', async () => {
    mockInstance.get.mockResolvedValue({
      data: { status: 'pending', uuid: 'sug-1' },
    });
    const result = await getAISuggestions('file-uuid');
    expect(result?.tags).toEqual([]);
    expect(result?.collections).toEqual([]);
  });

  it('defaults status to pending when the backend omits it', async () => {
    mockInstance.get.mockResolvedValue({
      data: { suggested_tags: [], suggested_collections: [], uuid: 'sug-1' },
    });
    const result = await getAISuggestions('file-uuid');
    expect(result?.status).toBe('pending');
  });

  it('falls through uuid, then suggestion_id, then id for the suggestion identifier', async () => {
    mockInstance.get.mockResolvedValue({
      data: {
        suggested_tags: [],
        suggested_collections: [],
        status: 'pending',
        suggestion_id: 'sug-fallback',
      },
    });
    const result = await getAISuggestions('file-uuid');
    expect(result?.suggestion_id).toBe('sug-fallback');
  });

  it('returns null when the response has no data', async () => {
    mockInstance.get.mockResolvedValue({ data: null });
    const result = await getAISuggestions('file-uuid');
    expect(result).toBeNull();
  });

  it('returns null on a 404 (no suggestions generated yet) instead of throwing', async () => {
    const notFound = { response: { status: 404 } };
    mockInstance.get.mockRejectedValue(notFound);
    const result = await getAISuggestions('file-uuid');
    expect(result).toBeNull();
  });

  it('rethrows non-404 errors', async () => {
    const serverError = { response: { status: 500 } };
    mockInstance.get.mockRejectedValue(serverError);
    await expect(getAISuggestions('file-uuid')).rejects.toBe(serverError);
  });
});

describe('extractAISuggestions', () => {
  it('posts to the extract endpoint with force_regenerate defaulted to false', async () => {
    mockInstance.post.mockResolvedValue({ data: undefined });
    await expect(extractAISuggestions('file-uuid')).resolves.toBeUndefined();
    expect(mockInstance.post).toHaveBeenCalledWith('/files/file-uuid/extract', {
      force_regenerate: false,
    });
  });

  it('passes force_regenerate through when the caller requests it', async () => {
    mockInstance.post.mockResolvedValue({ data: undefined });
    await expect(extractAISuggestions('file-uuid', true)).resolves.toBeUndefined();
    expect(mockInstance.post).toHaveBeenCalledWith('/files/file-uuid/extract', {
      force_regenerate: true,
    });
  });
});
