import { describe, it, expect, vi, beforeEach } from 'vitest';

/**
 * Transport-only client (#1204): these pin the wire shape of each call (method, path, body)
 * and that `response.data` comes back unchanged.
 */
const mockInstance = vi.hoisted(() => ({
  get: vi.fn(),
  put: vi.fn(),
  post: vi.fn(),
  delete: vi.fn(),
}));

vi.mock('$lib/axios', async () => {
  const actual = await vi.importActual<typeof import('$lib/axios')>('$lib/axios');
  return { ...actual, default: mockInstance };
});

import {
  getPyannoteCredential,
  savePyannoteCredential,
  deletePyannoteCredential,
  testPyannoteCredential,
} from './pyannoteCredential';

const STATUS = {
  configured: true,
  locked: false,
  test_status: null,
  test_message: null,
  last_tested: null,
  updated_at: '2026-10-10T10:00:00Z',
};

beforeEach(() => {
  vi.clearAllMocks();
});

describe('pyannote credential client', () => {
  it('reads the status from the user-settings route', async () => {
    mockInstance.get.mockResolvedValue({ data: STATUS });

    const result = await getPyannoteCredential();

    expect(mockInstance.get).toHaveBeenCalledWith('/user-settings/diarization/pyannote');
    expect(result).toEqual(STATUS);
  });

  it('puts the key as api_key and returns the status, which carries no key', async () => {
    mockInstance.put.mockResolvedValue({ data: STATUS });

    const result = await savePyannoteCredential('test-key-dddd-4444');

    expect(mockInstance.put).toHaveBeenCalledWith('/user-settings/diarization/pyannote', {
      api_key: 'test-key-dddd-4444',
    });
    expect(result).toEqual(STATUS);
    expect(JSON.stringify(result)).not.toContain('test-key-dddd-4444');
  });

  it('deletes and reports whether the source was reverted', async () => {
    const deleted = { deleted: true, diarization_source: 'provider', source_reverted: true };
    mockInstance.delete.mockResolvedValue({ data: deleted });

    const result = await deletePyannoteCredential();

    expect(mockInstance.delete).toHaveBeenCalledWith('/user-settings/diarization/pyannote');
    expect(result).toEqual(deleted);
  });

  it('tests the saved key with an empty body', async () => {
    const outcome = { success: true, code: 'connected', message: 'x', response_time_ms: 12 };
    mockInstance.post.mockResolvedValue({ data: outcome });

    const result = await testPyannoteCredential();

    expect(mockInstance.post).toHaveBeenCalledWith('/user-settings/diarization/pyannote/test', {});
    expect(result).toEqual(outcome);
  });

  it('tests an unsaved key by sending it in the body', async () => {
    const outcome = { success: false, code: 'rejected', message: 'x', response_time_ms: 3 };
    mockInstance.post.mockResolvedValue({ data: outcome });

    const result = await testPyannoteCredential('test-key-eeee-5555');

    expect(mockInstance.post).toHaveBeenCalledWith('/user-settings/diarization/pyannote/test', {
      api_key: 'test-key-eeee-5555',
    });
    expect(result.code).toBe('rejected');
    expect(result.success).toBe(false);
  });
});
